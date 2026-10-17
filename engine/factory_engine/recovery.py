"""Crash recovery (design: failure handling, "Engine crash or machine restart: continue from the
last completed step in `run.json`").

The pipeline saves the last completed step of each stage attempt in `run.json` (`Step`), and
`attempt.json` in the run folder (`Recovery`) holds what a restart needs that `run.json` doesn't:

- `attempt` (`AttemptRecord`): the attempt in flight, or the last one. Written once its
  post-stage baseline is taken, before its agent starts: role, number, baseline, prompt, the
  role's session before it began, whether it resumed that session, whether a rebase waited for
  the builder, and the reviewer's try in its review round.
- `retry` (`RetryNote`): what the next attempt is told. Written before a failed gate's step is
  saved, and when a review or a hook sends the run back to the builder.

`resume_point` says where a run that a crash or Ctrl-C left in `pending` or a working state
carries on; `Pipeline.recover` acts on it. By the last completed step of the run's stage:

| last step   | carries on with                                                                |
| ----------- | ------------------------------------------------------------------------------ |
| `started`   | `carry_on` with a record: the check against its saved baseline, then its own   |
|             | session carries on (or, with no session of its own yet, the attempt starts     |
|             | again with its saved prompt). `redo` without one: its agent never started.     |
| `agent`     | `check`: the post-stage check against the saved baseline, then the gate        |
| `checked`   | `gate`: the gate                                                               |
| `passed`    | `commit`: the stage commit (unless it was made), then the next state           |
| `committed` | the reviewer: `publish`; any other role: its stage was sent back (`advance`)   |
| `failed`    | `advance`: the next attempt, told what `retry` holds                           |
| `update`    | `review`: on to the review, the update isn't run again                         |

Anything else (`pending`, a step of an earlier stage, the update not done) is `advance`: the
state's step starts as usual. So no stage that passed runs again, and the same attempt carries
on rather than a new one, within the caps. A run from before this file existed has no record:
an agent that started then gets a new attempt, as before.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, cast, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from factory_engine.errors import GateError, PipelineError, RunFileError
from factory_engine.gates.post_stage import Baseline, MainState
from factory_engine.run import ATTEMPT_FILE, Role, Run, RunState
from factory_engine.testrun import write_atomic

StepName = Literal["started", "agent", "checked", "passed", "failed", "committed"]
STEP_NAMES: tuple[StepName, ...] = get_args(StepName)
UPDATE_STEP = "update"
"""The step label saved once the update before review (`Hooks.update`) is done."""
STAGE_ROLES: Mapping[RunState, Role] = {
    "planning": "planner",
    "testing": "tester",
    "building": "builder",
    "reviewing": "reviewer",
}
RECOVERABLE: tuple[RunState, ...] = (
    "pending",
    "planning",
    "testing",
    "building",
    "updating",
    "reviewing",
)
"""The states a crash or Ctrl-C leaves an interrupted run in."""
CRASH_PROMPT = (
    "The factory engine stopped while you were working on this (a crash or a restart). Check "
    "what you have already done, then carry on with your task from where you stopped."
)
_ROLES: tuple[Role, ...] = get_args(Role)
_ATTEMPT = re.compile(r"[1-9][0-9]{0,8}")

type Action = Literal["advance", "redo", "carry_on", "check", "gate", "commit", "publish", "review"]


@dataclass(frozen=True)
class Step:
    """A completed step of one stage attempt, saved in `run.json` as its `label`:
    `started` (attempt counted), `agent` (the agent finished), `checked` (post-stage check
    passed), `passed` or `failed` (the gate), `committed` (the stage commit)."""

    role: Role
    attempt: int
    name: StepName

    @property
    def label(self) -> str:
        return f"{self.role} {self.attempt} {self.name}"

    @classmethod
    def parse(cls, label: str | None) -> "Step | None":
        """The step a label names; None for no label, `UPDATE_STEP` or anything else."""
        parts = (label or "").split(" ")
        if len(parts) != 3:
            return None
        role, attempt, name = parts
        if role not in _ROLES or name not in STEP_NAMES or not _ATTEMPT.fullmatch(attempt):
            return None
        return cls(cast(Role, role), int(attempt), cast(StepName, name))


# ------------------------------------------------------------------ attempt.json


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


type Entry = tuple[str, int, int, int]


class SavedBaseline(_Model):
    """A post-stage `Baseline` as JSON."""

    head: str
    index: str
    main_files: dict[str, Entry]
    watched: dict[str, Entry]
    run_folder: dict[str, Entry]
    base: str

    @classmethod
    def of(cls, baseline: Baseline) -> "SavedBaseline":
        return cls(
            head=baseline.main.head,
            index=baseline.main.index,
            main_files=dict(baseline.main.files),
            watched=dict(baseline.watched),
            run_folder=dict(baseline.run_folder),
            base=baseline.base,
        )

    def restore(self) -> Baseline:
        return Baseline(
            main=MainState(head=self.head, index=self.index, files=dict(self.main_files)),
            watched=dict(self.watched),
            run_folder=dict(self.run_folder),
            base=self.base,
        )


class AttemptRecord(_Model):
    """One stage attempt, saved before its agent starts."""

    role: Role
    attempt: Annotated[int, Field(ge=1)]
    baseline: SavedBaseline
    prompt: str  # what the attempt's agent was told
    session_before: str | None  # the role's saved session when the attempt began
    resumed: bool  # the attempt resumed the role's saved session
    resolving: bool = False  # builder: a rebase onto main waited for it to resolve conflicts
    review_try: Annotated[int, Field(ge=0)] = 0  # reviewer: its try in the review round

    def own_session(self, run: Run) -> str | None:
        """The session this attempt's agent works in, once it is known: the session it resumed,
        or a new one saved since it began. None when its agent never got a session."""
        session = run.session_ids.get(self.role)
        if session is None or (not self.resumed and session == self.session_before):
            return None
        return session


class RetryNote(_Model):
    """What the next attempt of `role` is told: why the last one failed (`reason`) and, for the
    builder, the message its session is resumed with."""

    role: Role
    reason: str | None = None
    message: str | None = None


class Recovery(_Model):
    """`attempt.json`: the attempt in flight or last run, and what the next attempt is told."""

    attempt: AttemptRecord | None = None
    retry: RetryNote | None = None


def load_recovery(folder: Path) -> Recovery:
    """The run's `attempt.json`; empty when there is none (no attempt has started yet)."""
    path = folder / ATTEMPT_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Recovery()
    except (OSError, UnicodeDecodeError) as err:
        raise RunFileError(f"{path}: can't read ({err}). Check the run folder.") from err
    try:
        return Recovery.model_validate_json(text)
    except ValidationError as err:
        raise RunFileError(
            f"{path}: invalid attempt record ({err.error_count()} problems), so the engine "
            "can't tell what the last attempt did."
        ) from err


def save_recovery(folder: Path, recovery: Recovery) -> None:
    """Write `attempt.json` atomically."""
    path = folder / ATTEMPT_FILE
    try:
        write_atomic(path, recovery.model_dump_json(indent=2) + "\n")
    except GateError as err:
        raise RunFileError(str(err)) from err


# ------------------------------------------------------------------ where to carry on


@dataclass(frozen=True)
class ResumePoint:
    """Where an interrupted run carries on. `step` is its stage's last completed step, `record`
    the attempt that step belongs to (when saved), `review_tries` the reviewer's tries already
    used in the current review round."""

    action: Action
    step: Step | None = None
    record: AttemptRecord | None = None
    review_tries: int = 0


def resume_point(run: Run, recovery: Recovery, *, agent_started: bool) -> ResumePoint:
    """Where `run` carries on, from its last completed step and `recovery`. `agent_started`:
    a `stage_started` event names the last step's attempt (its agent was started)."""
    if run.state not in RECOVERABLE:
        raise PipelineError(
            f"run {run.repo}/{run.id} is {run.state}, not interrupted in pending or a working "
            "state, so there is nothing to recover."
        )
    if run.state == "updating":
        return ResumePoint("review" if run.last_completed_step == UPDATE_STEP else "advance")
    step = Step.parse(run.last_completed_step)
    role = STAGE_ROLES.get(run.state)
    if role is None or step is None or step.role != role:
        return ResumePoint("advance", step)
    record = recovery.attempt
    if record is not None and (record.role, record.attempt) != (step.role, step.attempt):
        record = None
    tries = record.review_try if record is not None and role == "reviewer" else 0
    match step.name:
        case "started" if record is not None:
            return ResumePoint("carry_on", step, record, max(tries - 1, 0))
        case "started":
            return ResumePoint(
                "advance" if agent_started else "redo", step, review_tries=_tried(recovery, role)
            )
        case "agent" | "checked" if record is not None:
            action: Action = "check" if step.name == "agent" else "gate"
            return ResumePoint(action, step, record, max(tries - 1, 0))
        case "agent" | "checked":
            return ResumePoint("advance", step)  # no record (an older run): a new attempt
        case "passed":
            return ResumePoint("commit", step, record)
        case "committed":
            return ResumePoint("publish" if role == "reviewer" else "advance", step)
        case "failed":
            return ResumePoint("advance", step, record, tries)
    raise PipelineError(f"step {step.label} has no resume point. This is an engine bug.")


def _tried(recovery: Recovery, role: Role) -> int:
    """For an attempt that never got its record: the reviewer's tries used in the round, from
    the last attempt's record when it was the reviewer's and told the next one to retry."""
    record, retry = recovery.attempt, recovery.retry
    if role != "reviewer" or record is None or record.role != "reviewer":
        return 0
    return record.review_try if retry is not None and retry.role == "reviewer" else 0
