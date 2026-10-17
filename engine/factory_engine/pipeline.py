"""The stage pipeline (design: run flow, gates between stages, failure handling).

`Pipeline.advance` carries a run through its stages, planner → tester → builder → reviewer,
until it waits for you or stops. One attempt of a stage is:

1. Before a retry, archive what the next attempt overwrites: `handoff-builder.md` is copied to
   `attempts/handoff-builder-<n>.md`, and `review.md` is moved to `attempts/review-<n>.md` (moved,
   so a reviewer that writes nothing can never pass on the old review). Likewise an earlier
   `plan.md` (failed, sent back or answered) is moved to `attempts/plan-<n>.md`.
2. Count the attempt, write the agent's config folder and take the post-stage baseline.
3. Run the agent through the `AgentRunner` under the watchdog. A usage limit pauses the run and,
   after the reset, resumes the same session in the same attempt. A stall (needs_you) or a turn
   or time cap (paused) stops the pipeline, and so does a runner that fails (`RUNNER_ERRORS`:
   needs_you, with the error); any other exception is an engine bug and is raised.
4. The post-stage check (`gates/post_stage.py`), against the attempt's baseline, after every
   agent run however it ended. A failure stops the run for you, no retry, and wins over a pause.
5. The stage's gate. On a pass the engine commits the stage (`git_ops.commit_stage`; empty for
   the planner and the reviewer) and moves the run on. On a fail it tries again within the cap:
   - planner and tester: a new session, told the gate's reason;
   - builder: its session resumed with the test output (`BuildGateOutcome.resume_message`);
     a flagged fail (test lock, paths it may not write) stops the run for you instead;
   - reviewer: Important findings resume the builder with `review.md`, within the builder's
     cap; a malformed `review.md` gives the reviewer a new session.
   A role that reaches its cap stops the run for you, as does a gate that can't run.

`run.json` holds the last completed step after every step (`Step`), and `attempt.json` the
attempt in flight (`recovery.py`), so a restart can tell where the run stopped:
`Pipeline.recover` carries an interrupted run on from there (task 2.24).

Where later tasks plug in:

- Plan approval (2.21): every plan waits in `awaiting_plan_approval`. `PipelineResult.stop` says
  whether the plan asks questions or proposes a split (`plan_stop`). A decision
  (`decisions.py`) moves the run (to `testing`, or back to `planning`); the caller then builds
  a pipeline from the saved run and calls `advance` again, and `advance(note=...)` hands the
  decision's note to the next agent.
- Update before review, pull request and merge (2.22, `merge.GitFlow`): `Hooks.update` runs in
  `updating`; by default it does nothing and the run goes on to `reviewing`. A rebase conflict
  sends the run back to the builder (`Pipeline.tell_builder`): while a rebase waits in the
  worktree, each builder attempt resolves conflicts, then the engine carries the rebase on
  (`git_ops.rebase_continue`) and the builder gate reruns the tests on the result. Tests that
  still fail after a resolution, or a stop for you mid-rebase (which gives the rebase up), need
  you. `Hooks.publish` runs once the review passes, before `awaiting_merge` (push and pull
  request). `Hooks.merge` runs in `awaiting_merge` only once your approval is recorded
  (`decisions.approve_merge`); the approval is used once, whatever the merge's outcome.
- Feedback (2.23): `advance` starts from whatever working state the run is in, with a new
  attempt of that stage. Crash recovery (2.24) is `recover`.
"""

import contextlib
import os
import shutil
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Literal, get_args

from claude_agent_sdk import ClaudeSDKError

from factory_engine.agent_config import SKILLS_DIR, agent_config_dir, write_agent_config
from factory_engine.agents.runner import (
    AgentResult,
    AgentRunner,
    AgentSpec,
    Outcome,
    RunIndex,
    SessionRecorder,
    build_spec,
    persist,
)
from factory_engine.agents.watchdog import (
    Limits,
    Sleep,
    Watchdog,
    resume_spec,
    unpause,
    wait_for_reset,
)
from factory_engine.config import RoleSettings, Settings
from factory_engine.errors import (
    AgentError,
    ConfigError,
    FactoryError,
    GateError,
    GitError,
    PipelineError,
    RunFileError,
)
from factory_engine.events import (
    EventLog,
    GateResult,
    NeedsYou,
    StageFinished,
    StageStarted,
    read_events,
)
from factory_engine.gates.build import BuildGateOutcome, check_build
from factory_engine.gates.plan import PLAN_FILE, PlanCheck, check_plan
from factory_engine.gates.post_stage import (
    Baseline,
    Watch,
    check_stage,
    post_stage_check,
    take_baseline,
)
from factory_engine.gates.review import check_review_file, record_findings
from factory_engine.gates.tests_gate import (
    check_tests,
    load_tester_files,
    rebase_tester_files,
)
from factory_engine.git_ops import (
    Git,
    commit_stage,
    rebase_abort,
    rebase_continue,
    rebase_in_progress,
    stage_message,
)
from factory_engine.providers import Profile
from factory_engine.recovery import (
    CRASH_PROMPT,
    UPDATE_STEP,
    AttemptRecord,
    Recovery,
    RetryNote,
    SavedBaseline,
    Step,
    StepName,
    UpdateProgress,
    load_recovery,
    resume_point,
    save_recovery,
)
from factory_engine.run import Pause, Role, Run, RunState
from factory_engine.safety.hook import HookPolicy, make_safety_hook
from factory_engine.safety.secrets import redact
from factory_engine.states import move
from factory_engine.testrun import DEFAULT_TIMEOUT, write_atomic

REVIEW_FILE = "review.md"
HANDOFF_BUILDER = "handoff-builder.md"
ATTEMPTS_DIR = "attempts"
REVIEW_DIFF = "review-diff.patch"
BASE_REF = "refs/remotes/origin/main"
"""The commit the reviewer's diff starts from is where the run branch meets this ref."""
REVIEWER_ATTEMPTS = 3
"""Reviewer sessions per review round: a malformed `review.md` gets two more tries."""
RUNNER_ERRORS: tuple[type[Exception], ...] = (ClaudeSDKError, AgentError, OSError)
"""What a failing agent runner raises: the SDK's errors (a dropped connection, the CLI missing
or exiting), the runner's own `AgentError` and OS errors (network, process, pipes). Anything
else that escapes an agent run is an engine bug and is raised."""
UPDATE_GATE = "update"
"""The `gate_result` of the update before review: the rebase and the tests rerun after it."""
TEST_LOCK_GATE = "update.test_lock"
"""The `gate_result` saying the test lock now follows main's version of a tester's file."""

PlanStop = Literal["plan_approval", "questions", "split"]
Stop = PlanStop | Literal["needs_you", "paused", "awaiting_merge", "ended"]
ROLES: tuple[Role, ...] = get_args(Role)
_NEXT: Mapping[Role, RunState] = {
    "planner": "awaiting_plan_approval",
    "tester": "building",
    "builder": "updating",
}
"""Where a passing stage goes once committed; the reviewer's stage publishes first."""
_STOPS: Mapping[RunState, Stop] = {
    "needs_you": "needs_you",
    "paused": "paused",
    "awaiting_merge": "awaiting_merge",
}


def plan_stop(plan: PlanCheck) -> PlanStop:
    """Which decision a plan waits for: a split first (it needs you in milestone 2), then
    questions, else plan approval."""
    if plan.split:
        return "split"
    return "questions" if plan.questions else "plan_approval"


@dataclass(frozen=True)
class Places:
    """Where one run lives."""

    repo: Path  # the main checkout
    data: Path  # the factory data folder
    worktree: Path
    run_folder: Path
    prompts: Path  # the factory's `prompts/`
    skills: Path  # the factory skills (`plugin/skills/`)


@dataclass(frozen=True)
class PipelineResult:
    """Where the pipeline stopped. `plan` is the plan gate's reading while the run waits for
    plan approval; `stop` then says whether the plan asks questions or proposes a split."""

    run: Run
    stop: Stop
    plan: PlanCheck | None = None


class Hooks:
    """Hook points for the git flow (2.22, `merge.GitFlow`). Each returns the run as it leaves
    it (use `Pipeline.move_to`, `stop_for_you` and `record`), and the pipeline saves that run
    before acting on its state; the defaults push, open and merge nothing."""

    async def update(self, pipeline: "Pipeline", run: Run) -> Run:
        """In `updating`, before the review: rebase onto main and rerun the tests. Leave the run
        in `updating` to go on to the review, or move it (to `building` on a conflict, to
        needs_you)."""
        return run

    async def publish(self, pipeline: "Pipeline", run: Run) -> Run:
        """After the review passed, still in `reviewing`: push and open the pull request. Leave
        the run in `reviewing` to go on to `awaiting_merge`, or move it."""
        return run

    async def merge(self, pipeline: "Pipeline", run: Run) -> Run:
        """In `awaiting_merge`, with your approval recorded: merge, and move the run to
        `merged` or needs_you. The default can't merge, so the run needs you."""
        return pipeline.stop_for_you(
            None, "this engine has no merge step, so nothing was merged. Merge it yourself."
        )


@dataclass(frozen=True)
class _AgentStop:
    """An agent that didn't finish: the watchdog stopped it, or its runner failed."""

    outcome: Outcome
    detail: str  # why, for you
    failed: bool  # the runner failed; the run is still in its working state


@dataclass(frozen=True)
class _ReviewDiff:
    base: str
    head: str
    path: Path


@dataclass(frozen=True)
class _Carry:
    """An attempt that carries on instead of a new one starting: one that paused
    (`resume_paused`) or one a crash or Ctrl-C interrupted (`recover`)."""

    role: Role
    attempt: int
    pause: Pause | None = None  # a paused attempt: its session carries on (`resume_spec`)
    note: str | None = None  # your note for the paused attempt
    record: AttemptRecord | None = None  # an interrupted attempt's record
    session: str | None = None  # the interrupted attempt's own session, to carry on
    prompt: str | None = None  # what the interrupted attempt's agent is told now
    finished: StepName | None = None  # `agent` or `checked`: its agent had finished


class Pipeline:
    """Runs one run's stages. Everything it uses is passed in: the run's places, its effective
    settings (bootstrap applied for run 0), the provider profile, the engine's environment
    (`os.environ`; agents and test commands get only what they need from it), git, the agent
    runner, the run's event log, the registry, a clock and a sleep."""

    def __init__(
        self,
        run: Run,
        *,
        places: Places,
        settings: Settings,
        profile: Profile,
        parent_env: Mapping[str, str],
        git: Git,
        runner: AgentRunner,
        events: EventLog,
        registry: RunIndex,
        clock: Callable[[], datetime],
        sleep: Sleep,
        hooks: Hooks | None = None,
        test_timeout: float = DEFAULT_TIMEOUT,
        base_ref: str = BASE_REF,
    ) -> None:
        if (events.repo, events.run_id) != (run.repo, run.id):
            raise PipelineError(
                f"event log for run {events.repo}/{events.run_id} can't record run "
                f"{run.repo}/{run.id}. Pass the run's own event log."
            )
        self._run = run
        self.places = places
        self.settings = settings
        self.git = git
        self.events = events
        self._profile = profile
        self._parent_env = parent_env
        self._runner = runner
        self._registry = registry
        self._clock = clock
        self._sleep = sleep
        self._hooks = hooks or Hooks()
        self._test_timeout = test_timeout
        self._base_ref = base_ref
        self._watchdog = Watchdog(Limits.from_caps(settings.caps), clock=clock, sleep=sleep)
        self._note: str | None = None
        self._builder_message: str | None = None
        self._carry: _Carry | None = None  # the attempt the next attempt of its role carries on
        self._retry: RetryNote | None = None  # what a recovered run's next attempt is told
        self._review_tries = 0  # the reviewer's tries already used in a recovered review round

    @property
    def run(self) -> Run:
        """The run as last saved."""
        return self._run

    def tell_builder(self, message: str) -> None:
        """What the builder is told when it next resumes (a hook's reason to send it back).
        Saved in `attempt.json` too, so a restart still tells it."""
        self._builder_message = message
        self._save_retry(RetryNote(role="builder", message=message))

    # ------------------------------------------------------------------ for the hooks

    def move_to(self, target: RunState) -> Run:
        """Move the run (logged and saved); returns it."""
        self._move(target)
        return self._run

    def stop_for_you(self, role: Role | None, reason: str) -> Run:
        """Move the run to needs_you with `reason` (logged and saved); returns it."""
        self._needs_you(role, reason)
        return self._run

    def record(self, run: Run) -> Run:
        """Save `run` (`run.json` and the registry) as the pipeline's run; returns it."""
        self._persist(run)
        return run

    def update_progress(self) -> UpdateProgress | None:
        """How far the update's rebase got before a restart (`attempt.json`); None if no
        rebase of this update was started."""
        return load_recovery(self.places.run_folder).update

    def save_update_progress(self, progress: UpdateProgress | None) -> None:
        """Save how far the update's rebase got, so a restart can go on from there."""
        saved = load_recovery(self.places.run_folder)
        self._save_recovery(saved.model_copy(update={"update": progress}))

    def attempts_left(self, role: Role) -> int:
        """How many attempts `role` has left under its cap in this run."""
        return max(0, self._cap(role) - self._attempts(role))

    def build_gate(self, base: str) -> BuildGateOutcome:
        """The builder gate on the worktree as it is: the test lock, the paths changed since
        `base` and the repo's tests, run by the engine. The test output is saved as the
        builder's latest attempt's (`test-output-builder-<n>.txt`). Raises ConfigError,
        GateError or GitError when it can't run."""
        return check_build(
            self.git,
            worktree=self.places.worktree,
            run_folder=self.places.run_folder,
            base=base,
            builder=self.settings.roles.builder,
            command=self.settings.commands.test,
            attempt=max(1, self._attempts("builder")),
            events=self.events,
            parent_env=self._parent_env,
            tester_stage=self._run.kind != "bootstrap",
            timeout=self._test_timeout,
        )

    # ------------------------------------------------------------------ the loop

    async def advance(self, *, note: str | None = None) -> PipelineResult:
        """Run stages from the run's current state until it waits for you or stops. `note`
        (your words) goes to the first agent this call starts."""
        self._note = note
        handlers: Mapping[RunState, Callable[[], Awaitable[None]]] = {
            "pending": self._start,
            "planning": self._plan,
            "testing": self._test,
            "building": self._build,
            "updating": self._update,
            "reviewing": self._review,
        }
        while (handler := self._handler(handlers)) is not None:
            state = self._run.state
            await handler()
            if self._run.state == state:
                raise PipelineError(
                    f"run {self._run.repo}/{self._run.id}: the {state} step ended with the run "
                    f"still {state}. This is an engine bug; mark the run Needs you by hand."
                )
        return self._result()

    async def resume_paused(self, *, note: str | None = None) -> PipelineResult:
        """Carry on a paused run: the attempt it paused in continues in the same session (told
        to carry on, plus `note`), without counting a new attempt, and then the stage goes on as
        usual (the post-stage check, the gate). A pause with no saved session starts a new
        attempt instead. A usage-limit pause is refused before its reset time."""
        pause = self._run.pause
        self._run = unpause(
            self._run,
            now=self._clock(),
            events=self.events,
            run_folder=self.places.run_folder,
            registry=self._registry,
        )
        if pause is None or pause.session_id is None:
            return await self.advance(note=note)
        self._carry = _Carry(pause.role, pause.attempt, pause=pause, note=note)
        try:
            return await self.advance()
        finally:
            self._carry = None

    async def recover(self, *, note: str | None = None) -> PipelineResult:
        """Carry on a run that a crash or Ctrl-C left in pending or a working state, from the
        last completed step in its `run.json` (`recovery.resume_point`): no stage that passed
        runs again. An attempt whose agent was working is checked against its saved baseline,
        then carries on in its own session, still the same attempt; one whose agent had
        finished goes on to its check or gate. `note` goes to the first agent that runs."""
        try:
            saved = load_recovery(self.places.run_folder)
        except RunFileError as err:
            self._needs_you(self._run.stage, f"can't tell where the run stopped: {err}")
            return self._result()
        step = Step.parse(self._run.last_completed_step)
        point = resume_point(self._run, saved, agent_started=self._agent_started(step))
        self._retry, self._review_tries = saved.retry, point.review_tries
        record = point.record
        try:
            if point.action == "redo" and step is not None:
                # Counted, but its agent never started: the same attempt starts again.
                attempts = {**self._run.attempts, step.role: step.attempt - 1}
                self._run = self._run.model_copy(update={"attempts": attempts})
            elif point.action == "carry_on" and record is not None:
                session = record.own_session(self._run)
                prompt = crash_prompt(record.prompt) if session else record.prompt
                self._carry = _Carry(
                    record.role,
                    record.attempt,
                    record=record,
                    session=session,
                    prompt=_with_note(prompt, note),
                )
                note = None
            elif point.action in ("check", "gate") and record is not None and step is not None:
                self._carry = _Carry(record.role, record.attempt, record=record, finished=step.name)
            elif point.action == "commit" and step is not None:
                committed = self._stage_committed(step)
                if committed is not None:
                    await self._pass_stage(step.role, committed=committed)
            elif point.action == "publish":
                await self._publish()
            elif point.action == "review":
                self._move("reviewing")
            return await self.advance(note=note)
        finally:
            self._carry, self._retry, self._review_tries = None, None, 0

    def _agent_started(self, step: Step | None) -> bool:
        """Whether a `stage_started` event names `step`'s attempt: its agent was started."""
        if step is None or step.name != "started":
            return False
        return any(
            isinstance(event.body, StageStarted)
            and (event.body.stage, event.body.attempt) == (step.role, step.attempt)
            for event in read_events(self.places.run_folder)
        )

    def _stage_committed(self, step: Step) -> bool | None:
        """Whether the worktree's head is already `step`'s stage commit (the engine died after
        committing, before it saved the step); None when that can't be told (needs you)."""
        try:
            subject = self.git.out(["log", "-1", "--format=%s", "HEAD"], self.places.worktree)
        except GitError as err:
            self._needs_you(step.role, f"can't tell whether the {step.role} was committed: {err}")
            return None
        return subject == stage_message(self._run.id, step.role, step.attempt)

    def _continues(self, role: Role) -> bool:
        """Whether `role`'s next attempt carries on a paused or interrupted one."""
        return self._carry is not None and self._carry.role == role

    def _seeded(self, role: Role) -> RetryNote | None:
        """What a recovered run's next attempt of `role` is told, once."""
        retry = self._retry
        if retry is None or retry.role != role:
            return None
        self._retry = None
        return retry

    def _handler(
        self, handlers: Mapping[RunState, Callable[[], Awaitable[None]]]
    ) -> Callable[[], Awaitable[None]] | None:
        """The step for the run's state. A run in `awaiting_merge` waits, unless your approval
        is recorded: then it merges."""
        if self._run.state == "awaiting_merge" and self._run.merge_approval is not None:
            return self._merge
        return handlers.get(self._run.state)

    def _result(self) -> PipelineResult:
        state = self._run.state
        if state == "awaiting_plan_approval":
            plan = check_plan(self.places.run_folder / PLAN_FILE)
            return PipelineResult(self._run, plan_stop(plan), plan)
        return PipelineResult(self._run, _STOPS.get(state, "ended"))

    async def _start(self) -> None:
        self._move("planning")

    # ------------------------------------------------------------------ the stages

    async def _plan(self) -> None:
        seeded = self._seeded("planner")
        reason = seeded.reason if seeded else None
        while self._may_try("planner", reason):
            prompt: str | None = None
            if not self._continues("planner"):
                previous = self._set_plan_aside()
                if previous is False:
                    return
                prompt = _planner_prompt(self.places, reason, self._take_note(), previous)
            if await self._attempt("planner", prompt, resume=False) is None:
                return
            check = check_plan(self.places.run_folder / PLAN_FILE)
            if self._gate("planner", check.event(), RetryNote(role="planner", reason=check.reason)):
                await self._pass_stage("planner")
                return
            reason = check.reason

    async def _test(self) -> None:
        seeded = self._seeded("tester")
        reason = seeded.reason if seeded else None
        while self._may_try("tester", reason):
            prompt = None
            if not self._continues("tester"):
                prompt = _tester_prompt(self.places, reason, self._take_note())
            baseline = await self._attempt("tester", prompt, resume=False)
            if baseline is None:
                return
            try:
                outcome = check_tests(
                    self.git,
                    worktree=self.places.worktree,
                    run_folder=self.places.run_folder,
                    base=baseline.base,
                    tester=self.settings.roles.tester,
                    command=self.settings.commands.test,
                    attempt=self._attempts("tester"),
                    events=self.events,
                    parent_env=self._parent_env,
                    timeout=self._test_timeout,
                )
            except (ConfigError, GateError, GitError) as err:
                self._needs_you("tester", f"the tester gate couldn't run: {err}")
                return
            if self._gate(
                "tester", outcome.event(), RetryNote(role="tester", reason=outcome.reason)
            ):
                await self._pass_stage("tester")
                return
            reason = outcome.reason

    async def _build(self) -> None:
        await self._build_attempts()
        if self._run.state == "needs_you":
            self._give_up_rebase()

    async def _build_attempts(self) -> None:
        seeded = self._seeded("builder")
        if seeded is not None and self._builder_message is None:
            self._builder_message = seeded.message
        message, self._builder_message = self._builder_message, None
        reason = seeded.reason if seeded else None
        while self._may_try("builder", reason):
            done = self._attempts("builder")
            carry = self._carry if self._continues("builder") else None
            kept = f"handoff-builder-{done}.md"
            if done > 0 and carry is None and not self._archive(HANDOFF_BUILDER, kept, "builder"):
                return
            resolving = self._rebasing()
            if resolving is None:
                return
            resume = done > 0 and "builder" in self._run.session_ids
            prompt = None
            if carry is None:
                prompt = _builder_prompt(self.places, message, self._take_note(), resume=resume)
            baseline = await self._attempt("builder", prompt, resume=resume, resolving=resolving)
            if baseline is None:
                return
            base = baseline.base
            if resolving:
                carried = self._carry_rebase_on()
                if carried is None:
                    return
                if isinstance(carried, tuple):
                    reason = f"the rebase onto main still has conflicts, in {', '.join(carried)}."
                    message = conflict_message(self.places, carried)
                    result = GateResult(gate=UPDATE_GATE, passed=False, reason=reason)
                    self._gate(
                        "builder", result, RetryNote(role="builder", reason=reason, message=message)
                    )
                    continue
                base = carried
            elif carry is not None and carry.finished and carry.record and carry.record.resolving:
                # The engine had carried the rebase on before it died: go on from its result.
                rebased = self._rebased_head()
                if rebased is None:
                    return
                base = rebased
            try:
                outcome = self.build_gate(base)
            except (ConfigError, GateError, GitError) as err:
                self._needs_you("builder", f"the builder gate couldn't run: {err}")
                return
            retry = RetryNote(
                role="builder", reason=outcome.reason, message=outcome.resume_message()
            )
            stop: str | None = None  # a fail that stops the run for you, not a retry
            if outcome.flagged:
                stop = outcome.reason
            elif resolving:
                stop = (
                    "the tests still fail after the builder resolved the conflicts of the rebase "
                    f"onto main: {outcome.reason}"
                )
            if self._gate("builder", outcome.event(), retry, stop=stop):
                await self._pass_stage("builder")
                return
            if stop is not None:
                return
            reason, message = outcome.reason, outcome.resume_message()

    def _rebasing(self) -> bool | None:
        """Whether a rebase onto main waits in the worktree for the builder to resolve its
        conflicts; None when that can't be told (the run stopped for you)."""
        run = self._run
        try:
            return rebase_in_progress(self.git, self.places.worktree, run_id=run.id, slug=run.slug)
        except GitError as err:
            self._needs_you("builder", f"can't tell whether a rebase waits in the worktree: {err}")
            return None

    def _carry_rebase_on(self) -> str | tuple[str, ...] | None:
        """After the builder edited the conflicted files: carry the rebase on. Returns the new
        head once the rebase is done, the files still (or newly) in conflict, or None when it
        failed (the run stopped for you)."""
        run = self._run
        places = self.places
        try:
            step = rebase_continue(self.git, places.worktree, run_id=run.id, slug=run.slug)
            if step.conflicts:
                locked = locked_conflicts(places.run_folder, step.conflicts)
                if locked:
                    self._needs_you("builder", locked_conflict_reason(locked))
                    return None
                return step.conflicts
            rebased = rebase_tester_files(self.git, places.run_folder, places.worktree)
            if rebased:
                self.events.append(lock_rebased(rebased), role="builder")
            return self.git.out(["rev-parse", "HEAD"], places.worktree)
        except (GitError, GateError) as err:
            self._needs_you("builder", f"the rebase onto main couldn't carry on: {err}")
            return None

    def _rebased_head(self) -> str | None:
        """After a restart: the head of the rebase the engine finished before it died, with the
        test lock following it (as `_carry_rebase_on` does); None when that failed."""
        places = self.places
        try:
            rebased = rebase_tester_files(self.git, places.run_folder, places.worktree)
            if rebased:
                self.events.append(lock_rebased(rebased), role="builder")
            return self.git.out(["rev-parse", "HEAD"], places.worktree)
        except (GitError, GateError) as err:
            self._needs_you(
                "builder", f"can't read the rebase onto main the engine finished: {err}"
            )
            return None

    def _give_up_rebase(self) -> None:
        """A run that stops for you mid-rebase gives the rebase up, so the run branch is back
        where it was before the update and the worktree isn't left half rebased."""
        run = self._run
        worktree = self.places.worktree
        try:
            if not rebase_in_progress(self.git, worktree, run_id=run.id, slug=run.slug):
                return
            rebase_abort(self.git, worktree, run_id=run.id, slug=run.slug)
            result = GateResult(
                gate=UPDATE_GATE,
                passed=False,
                reason="the rebase onto main was given up; the run branch is back where it was "
                "before the update.",
            )
        except GitError as err:
            result = GateResult(
                gate=UPDATE_GATE,
                passed=False,
                reason=f"the rebase onto main waits in {worktree} and couldn't be given up "
                f"({err}). Run `git rebase --abort` there before resuming the run.",
            )
        self.events.append(result, role="builder")

    async def _update(self) -> None:
        self._take_hooked(await self._hooks.update(self, self._run), "update")
        if self._run.state == "updating":
            self.save_update_progress(None)
            self._save(UPDATE_STEP)
            self._move("reviewing")

    async def _merge(self) -> None:
        """Your approval is used once: whatever the merge does, it is cleared after."""
        self._take_hooked(await self._hooks.merge(self, self._run), "merge")
        if self._run.merge_approval is not None:
            self._persist(self._run.model_copy(update={"merge_approval": None}))

    async def _review(self) -> None:
        seeded = self._seeded("reviewer")
        reason = seeded.reason if seeded else None
        tried, self._review_tries = self._review_tries, 0
        for tries in range(tried, REVIEWER_ATTEMPTS):
            prompt = None
            if not self._continues("reviewer"):
                # Not for an attempt that carries on: its run folder must stay as it left it.
                if not self._set_review_aside(self._attempts("reviewer")):
                    return
                diff = self._write_review_diff()
                if diff is None:
                    return
                prompt = _reviewer_prompt(self.places, diff, reason, self._take_note())
            if await self._attempt("reviewer", prompt, resume=False, review_try=tries + 1) is None:
                return
            outcome = check_review_file(self.places.run_folder / REVIEW_FILE)
            if not outcome.passed and outcome.retry == "builder":
                self.events.append(outcome.event(), role="reviewer")
                self._send_back(record_findings(self._run, outcome), outcome.reason)
                return
            if self._gate(
                "reviewer", outcome.event(), RetryNote(role="reviewer", reason=outcome.reason)
            ):
                await self._pass_stage("reviewer")
                return
            reason = outcome.reason
        self._needs_you(
            "reviewer",
            f"the reviewer wrote a malformed review.md {REVIEWER_ATTEMPTS} times in a row. "
            f"The last problem: {reason}",
        )

    def _take_hooked(self, run: Run, hook: str) -> None:
        """Save the run a hook returned (run.json and the registry) before acting on it."""
        if (run.repo, run.id) != (self._run.repo, self._run.id):
            raise PipelineError(
                f"the {hook} hook returned run {run.repo}/{run.id} for run "
                f"{self._run.repo}/{self._run.id}. Return the run it was given."
            )
        self._persist(run)

    def _send_back(self, run: Run, reason: str) -> None:
        """Important findings: resume the builder with `review.md`, within its cap. The
        findings, the reviewer's failed step and the move are saved together, so a restart
        never counts the findings twice or loses the send-back."""
        label = Step("reviewer", self._attempts("reviewer"), "failed").label
        self._run = run.model_copy(update={"last_completed_step": label})
        cap = self._cap("builder")
        if self._attempts("builder") >= cap:
            self._needs_you(
                "reviewer",
                f"the review failed ({reason}) and the builder has used all {cap} of its "
                f"attempts. The findings are in {self.places.run_folder / REVIEW_FILE}.",
            )
            return
        path = self.places.run_folder / REVIEW_FILE
        try:
            review = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as err:
            self._needs_you("reviewer", f"can't read {path} to send to the builder ({err}).")
            return
        self._builder_message = _review_message(path, review)
        self._save_retry(RetryNote(role="builder", reason=reason, message=self._builder_message))
        self._move("building")

    async def _pass_stage(self, role: Role, *, committed: bool = False) -> None:
        """After the stage's gate passed: its commit (unless `committed`, already made) and the
        next state, saved together; the reviewer's stage then publishes."""
        if not self._commit(role, _NEXT.get(role), made=committed):
            return
        if role == "reviewer":
            await self._publish()

    async def _publish(self) -> None:
        """Push and open the pull request (`Hooks.publish`), then wait for the merge."""
        self._take_hooked(await self._hooks.publish(self, self._run), "publish")
        if self._run.state == "reviewing":
            self._move("awaiting_merge")

    # ------------------------------------------------------------------ one attempt

    async def _attempt(
        self,
        role: Role,
        prompt: str | None,
        *,
        resume: bool,
        resolving: bool = False,
        review_try: int = 0,
    ) -> Baseline | None:
        """One attempt of `role`'s agent, up to and including the post-stage check. Returns the
        baseline when the check passed; None when the run stopped (it has moved). `prompt` is
        None only for an attempt that carries on (`_Carry`), which brings its own.

        After `resume_paused` the first attempt carries on the paused one: same attempt number,
        same session, told to carry on. After `recover` it carries on the interrupted one: the
        check against its saved baseline first, then its own session carries on (or, with none
        yet, it starts again with its saved prompt), keeping the stage's base commit for the
        checks and the gate; or, when its agent had finished, only what was left of it runs."""
        carry, self._carry = self._carry, None
        if carry is not None and carry.role != role:
            carry = None
        self._retry = None  # an attempt starts: anything a restart seeded is used or stale
        if carry is not None and carry.finished is not None and carry.record is not None:
            return self._finish_interrupted(role, carry.record, carry.finished)
        attempt = carry.attempt if carry is not None else self._attempts(role) + 1
        self._run = self._run.model_copy(
            update={
                "stage": role,
                "attempts": {**self._run.attempts, role: max(attempt, self._attempts(role))},
            }
        )
        self._save(Step(role, attempt, "started").label)
        places = self.places
        role_settings: RoleSettings = getattr(self.settings.roles, role)
        record = carry.record if carry is not None else None
        session_before = self._run.session_ids.get(role)
        try:
            locked, config_dir, policy = self._prepare(role)
            watch = self._watch(config_dir)
            if record is not None:
                # What the interrupted attempt did so far is checked before it carries on.
                checked = post_stage_check(
                    self.git,
                    watch,
                    record.baseline.restore(),
                    role=role,
                    settings=role_settings,
                    run=self._run,
                    events=self.events,
                    registry=self._registry,
                    locked=locked,
                )
                self._run = checked.run
                if not checked.passed:
                    return None
            fresh = record is not None and carry is not None and carry.session is None
            spec = build_spec(
                role=role,
                attempt=attempt,
                prompt=prompt or "",
                run=self._run,
                settings=self.settings,
                profile=self._profile,
                parent_env=self._parent_env,
                worktree=places.worktree,
                run_folder=places.run_folder,
                config_dir=config_dir,
                prompts_dir=places.prompts,
                hook=make_safety_hook(policy, self.events),
                resume=record.resumed if fresh and record else resume and carry is None,
            )
            if carry is not None and carry.pause is not None:
                spec = resume_spec(spec, carry.pause)
                if carry.note:
                    spec = replace(spec, prompt=f"{spec.prompt}\n\n{carry.note}")
            elif carry is not None:
                spec = replace(spec, prompt=carry.prompt or "")
                if carry.session is not None:
                    spec = replace(spec, resume=carry.session)
            elif prompt is None:
                raise PipelineError(f"the {role}'s attempt {attempt} has no prompt (engine bug).")
            baseline = take_baseline(self.git, watch)
            if record is not None:
                baseline = replace(baseline, base=record.baseline.base)
            self._save_recovery(
                Recovery(
                    attempt=AttemptRecord(
                        role=role,
                        attempt=attempt,
                        baseline=SavedBaseline.of(baseline),
                        prompt=record.prompt if record is not None else spec.prompt,
                        session_before=session_before,
                        resumed=spec.resume is not None,
                        resolving=resolving,
                        review_try=review_try,
                    )
                )
            )
        except FactoryError as err:
            self._needs_you(role, f"the {role}'s attempt {attempt} couldn't start: {err}")
            return None
        self.events.append(StageStarted(stage=role, attempt=attempt), role=role)
        result = await self._run_agent(spec)
        self.events.append(
            StageFinished(stage=role, attempt=attempt, outcome=result.outcome), role=role
        )
        if isinstance(result, _AgentStop):
            # Whatever stopped the agent, check what it left before the run waits.
            self._check_after_stop(role, watch, baseline, role_settings, locked, result)
            return None
        self._save(Step(role, attempt, "agent").label)
        checked = post_stage_check(
            self.git,
            watch,
            baseline,
            role=role,
            settings=role_settings,
            run=self._run,
            events=self.events,
            registry=self._registry,
            locked=locked,
        )
        self._run = checked.run
        if not checked.passed:
            return None
        if result.outcome != "completed":
            said = f" Its last answer: {result.result}" if result.result else ""
            self._needs_you(
                role,
                f"the {role}'s agent stopped without finishing its task ({result.outcome}).{said}",
            )
            return None
        self._save(Step(role, attempt, "checked").label)
        return baseline

    def _prepare(self, role: Role) -> tuple[tuple[str, ...], Path, HookPolicy]:
        """The files locked from `role`, its agent's config folder (written) and its hook
        policy. Raises FactoryError when one can't be made."""
        places = self.places
        locked = () if role == "tester" else tuple(load_tester_files(places.run_folder))
        config_dir = agent_config_dir(
            places.data, self._run.repo, self._run.id, self._run.slug, role
        )
        write_agent_config(
            config_dir,
            role=role,
            settings=self.settings,
            run_folder=places.run_folder,
            skills_source=places.skills,
        )
        policy = HookPolicy.build(
            role,
            self.settings,
            worktree=places.worktree,
            run_folder=places.run_folder,
            locked=locked,
            read_only=(config_dir / SKILLS_DIR,),
        )
        return locked, config_dir, policy

    def _watch(self, config_dir: Path) -> Watch:
        places = self.places
        return Watch.for_stage(
            repo=places.repo,
            data=places.data,
            worktree=places.worktree,
            run_folder=places.run_folder,
            config_dir=config_dir,
        )

    def _finish_interrupted(
        self, role: Role, record: AttemptRecord, finished: StepName
    ) -> Baseline | None:
        """An interrupted attempt whose agent had finished: after `agent`, the post-stage check
        against its saved baseline (and that the agent completed its task); after `checked`,
        nothing more. Returns its baseline for the gate; None when the run stopped."""
        baseline = record.baseline.restore()
        if finished != "agent":
            return baseline
        try:
            locked = () if role == "tester" else tuple(load_tester_files(self.places.run_folder))
            config_dir = agent_config_dir(
                self.places.data, self._run.repo, self._run.id, self._run.slug, role
            )
        except FactoryError as err:
            self._needs_you(role, f"the {role}'s attempt {record.attempt} can't be checked: {err}")
            return None
        checked = post_stage_check(
            self.git,
            self._watch(config_dir),
            baseline,
            role=role,
            settings=getattr(self.settings.roles, role),
            run=self._run,
            events=self.events,
            registry=self._registry,
            locked=locked,
        )
        self._run = checked.run
        if not checked.passed:
            return None
        outcome = self._finished_outcome(role, record.attempt)
        if outcome != "completed":
            self._needs_you(
                role,
                f"the {role}'s agent stopped without finishing its task ({outcome or 'unknown'}).",
            )
            return None
        self._save(Step(role, record.attempt, "checked").label)
        return baseline

    def _finished_outcome(self, role: Role, attempt: int) -> str | None:
        """How the agent of `role`'s attempt ended, from its last `stage_finished` event."""
        outcomes = [
            event.body.outcome
            for event in read_events(self.places.run_folder)
            if isinstance(event.body, StageFinished)
            and (event.body.stage, event.body.attempt) == (role, attempt)
        ]
        return outcomes[-1] if outcomes else None

    async def _run_agent(self, spec: AgentSpec) -> "AgentResult | _AgentStop":
        """Run the agent under the watchdog. A usage limit waits for the reset and resumes the
        same session. Returns an `_AgentStop` when the agent was stopped (a stall moved the run
        to needs_you, a cap paused it) or its runner failed (the run is still working)."""
        while True:
            recorder = SessionRecorder(
                self._run,
                run_folder=self.places.run_folder,
                events=self.events,
                registry=self._registry,
                role=spec.role,
                attempt=spec.attempt,
                clock=self._clock,
            )
            try:
                watched = await self._watchdog.run(self._runner, spec, recorder, events=self.events)
            except RUNNER_ERRORS as err:
                # The runner or the agent's connection failed. Keep what the recorder saved
                # (the session ID, tokens, a watchdog stop). Engine bugs aren't caught here.
                self._run = recorder.run
                detail = redact(f"{type(err).__name__}: {err}")
                return _AgentStop("failed", f"the {spec.role}'s agent failed ({detail})", True)
            self._run = watched.result.run
            trip = watched.trip
            if trip is None:
                return watched.result
            pause = self._run.pause
            if trip.kind != "usage_limit" or pause is None or pause.session_id is None:
                return _AgentStop(watched.result.outcome, trip.detail, False)
            await wait_for_reset(self._run, clock=self._clock, sleep=self._sleep)
            self._run = unpause(
                self._run,
                now=self._clock(),
                events=self.events,
                run_folder=self.places.run_folder,
                registry=self._registry,
            )
            spec = resume_spec(spec, pause)

    def _check_after_stop(
        self,
        role: Role,
        watch: Watch,
        baseline: Baseline,
        settings: RoleSettings,
        locked: tuple[str, ...],
        stop: "_AgentStop",
    ) -> None:
        """The post-stage check for an agent that didn't finish, against the attempt's own
        baseline, each check logged. A failed check stops the run for you whatever stopped the
        agent, and the reason says both; so does a runner failure. A watchdog stop with clean
        checks stays as the watchdog left it (needs_you for a stall, paused for a cap)."""
        results = check_stage(
            self.git, watch, baseline, role=role, settings=settings, locked=locked
        )
        for result in results:
            self.events.append(result, role=role)
        failed = [result for result in results if not result.passed]
        if failed:
            listed = " ".join(f"{result.gate}: {result.reason}" for result in failed)
            self._stop_for_you(
                role,
                f"the engine's check after the {role} stage failed. {listed} "
                f"The agent had stopped: {stop.detail}",
            )
        elif stop.failed:
            self._stop_for_you(
                role,
                f"{stop.detail}. The checks after the stage passed and the worktree is as the "
                "agent left it; resume the run or decide it.",
            )

    def _stop_for_you(self, role: Role, reason: str) -> None:
        """Needs you: from a working state, from paused (the pause no longer applies), or
        again from needs_you (the new reason is logged too)."""
        run = self._run
        if run.state != "needs_you":
            run = move(run, "needs_you", log=self.events)
        self.events.append(NeedsYou(reason=reason), role=role)
        self._persist(run.model_copy(update={"pause": None}))

    def _gate(
        self,
        role: Role,
        result: GateResult,
        retry: RetryNote | None = None,
        *,
        stop: str | None = None,
    ) -> bool:
        """Log the gate's result and save the step; whether it passed. On a fail, `retry` (what
        the next attempt is told) is saved first, so a restart after the step still tells it;
        with `stop`, the fail stops the run for you, saved in the same write as the step, so a
        restart can't take it for a retry."""
        self.events.append(result, role=role)
        if not result.passed and retry is not None and stop is None:
            self._save_retry(retry)
        name: StepName = "passed" if result.passed else "failed"
        label = Step(role, self._attempts(role), name).label
        if not result.passed and stop is not None:
            self._run = self._run.model_copy(update={"last_completed_step": label})
            self._needs_you(role, stop)
            return False
        self._save(label)
        return result.passed

    def _commit(self, role: Role, target: RunState | None, *, made: bool = False) -> bool:
        """The stage's one commit (unless `made`), then its step and the move to `target`,
        saved together. A failed commit stops the run for you."""
        run = self._run
        attempt = self._attempts(role)
        if not made:
            try:
                commit_stage(
                    self.git,
                    self.places.worktree,
                    run_id=run.id,
                    slug=run.slug,
                    role=role,
                    attempt=attempt,
                )
            except GitError as err:
                self._needs_you(role, f"the {role}'s stage passed but couldn't be committed: {err}")
                return False
        run = run.model_copy(update={"last_completed_step": Step(role, attempt, "committed").label})
        self._persist(move(run, target, log=self.events) if target is not None else run)
        return True

    def _save_recovery(self, recovery: Recovery) -> None:
        save_recovery(self.places.run_folder, recovery)

    def _save_retry(self, retry: RetryNote) -> None:
        """Save what the next attempt is told in `attempt.json`, next to the last attempt."""
        saved = load_recovery(self.places.run_folder)
        self._save_recovery(saved.model_copy(update={"retry": retry}))

    # ------------------------------------------------------------------ caps and stops

    def _cap(self, role: Role) -> int:
        caps = self.settings.caps
        if role == "planner":
            return caps.planner_attempts
        if role == "tester":
            return caps.tester_attempts
        if role == "builder":
            return caps.builder_attempts
        return REVIEWER_ATTEMPTS

    def _may_try(self, role: Role, reason: str | None) -> bool:
        """Whether `role` has an attempt left; if not, the run stops for you."""
        cap = self._cap(role)
        if self._attempts(role) < cap or self._continues(role):
            return True
        last = f" The last attempt failed: {reason}" if reason else ""
        self._needs_you(role, f"the {role} reached its cap of {cap} attempts.{last}")
        return False

    def _needs_you(self, role: Role | None, reason: str) -> None:
        moved = move(self._run, "needs_you", log=self.events)
        self.events.append(NeedsYou(reason=reason), role=role)
        self._persist(moved)

    def _move(self, target: RunState) -> None:
        self._persist(move(self._run, target, log=self.events))

    def _save(self, label: str) -> None:
        self._persist(self._run.model_copy(update={"last_completed_step": label}))

    def _persist(self, run: Run) -> None:
        persist(run, self.places.run_folder, self._registry)
        self._run = run

    def _attempts(self, role: Role) -> int:
        return self._run.attempts.get(role, 0)

    def _take_note(self) -> str | None:
        note, self._note = self._note, None
        return note

    # ------------------------------------------------------------------ run docs

    def _archive(self, name: str, archived: str, role: Role) -> bool:
        """Copy `<run folder>/name` to `attempts/archived`, if it exists. A copy that fails stops
        the run for you."""
        source = self.places.run_folder / name
        try:
            if source.is_file():
                _copy(source, self.places.run_folder / ATTEMPTS_DIR / archived)
        except OSError as err:
            self._needs_you(role, f"can't archive {source} before the next attempt ({err}).")
            return False
        return True

    def _set_plan_aside(self) -> Path | None | Literal[False]:
        """Before a planner runs: move an earlier `plan.md` (a failed one, or one you sent back
        or answered) to `attempts/plan-<n>.md`, so only a plan written by this attempt is
        judged. Returns where it went (None when there was none); False when the run stopped."""
        path = self.places.run_folder / PLAN_FILE
        archived = self.places.run_folder / ATTEMPTS_DIR / f"plan-{self._attempts('planner')}.md"
        try:
            if not path.is_file():
                return None
            _copy(path, archived)
            path.unlink()
        except OSError as err:
            self._needs_you("planner", f"can't set the old {path} aside ({err}).")
            return False
        return archived

    def _set_review_aside(self, done: int) -> bool:
        """Before a reviewer runs: archive the last `review.md` as `attempts/review-<done>.md`
        and remove it, so only a review written by this attempt is judged."""
        path = self.places.run_folder / REVIEW_FILE
        if done and not self._archive(REVIEW_FILE, f"review-{done}.md", "reviewer"):
            return False
        try:
            path.unlink(missing_ok=True)
        except OSError as err:
            self._needs_you("reviewer", f"can't remove the old {path} ({err}).")
            return False
        return True

    def _write_review_diff(self) -> _ReviewDiff | None:
        """The run's whole diff, from where the run branch meets `base_ref` to its head, for
        the reviewer (who doesn't run git)."""
        worktree = self.places.worktree
        path = self.places.run_folder / REVIEW_DIFF
        try:
            base = self.git.out(["merge-base", self._base_ref, "HEAD"], worktree)
            head = self.git.out(["rev-parse", "HEAD"], worktree)
            diff = self.git.run(
                ["diff", "--no-color", "--no-ext-diff", "--no-textconv", base, head, "--"],
                worktree,
            ).stdout
            write_atomic(path, diff)
        except (GitError, GateError) as err:
            self._needs_you("reviewer", f"can't write the diff for the review ({err}).")
            return None
        return _ReviewDiff(base, head, path)


def _copy(source: Path, target: Path) -> None:
    """Copy a file through a temp file and a replace, so `target` is never half written."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f"{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(fd)
    temp = Path(name)
    try:
        shutil.copyfile(source, temp)
        temp.replace(target)
    except BaseException:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)
        raise


# ------------------------------------------------------------------ prompts


def crash_prompt(prompt: str) -> str:
    """What an interrupted agent is told when its session carries on: what happened, then its
    attempt's task again, in case the task hadn't reached it."""
    return f"{CRASH_PROMPT}\n\nThis attempt's task, as you were first told it:\n\n{prompt}"


def _with_note(text: str, note: str | None) -> str:
    return f"{text}\n\nA note from the person running the factory:\n\n{note}" if note else text


def _planner_prompt(
    places: Places, reason: str | None, note: str | None, previous: Path | None
) -> str:
    text = (
        f"Plan this run. The run folder is {places.run_folder}: read intent.md there, then "
        f"write plan.md and handoff-planner.md there. The repo is the worktree, {places.worktree}."
    )
    if previous is not None:
        text += (
            f"\n\nThe previous plan was moved to {previous}; read it if it helps, but write a "
            "new plan.md: only the plan.md you write now is checked."
        )
    if reason:
        text += (
            "\n\nYour last plan.md failed the planner gate. Fix every problem and write "
            f"plan.md again:\n\n{reason}"
        )
    return _with_note(text, note)


def _tester_prompt(places: Places, reason: str | None, note: str | None) -> str:
    text = (
        "Write the tests for this run's plan. Read intent.md, plan.md and handoff-planner.md "
        f"in the run folder, {places.run_folder}. Write the tests in the worktree, "
        f"{places.worktree}, then handoff-tester.md in the run folder."
    )
    if reason:
        text += (
            "\n\nYour last tests failed the tester gate; your files are still in the worktree. "
            f"Fix them:\n\n{reason}"
        )
    return _with_note(text, note)


def _builder_prompt(places: Places, message: str | None, note: str | None, *, resume: bool) -> str:
    """A resumed builder gets why it is back; a new session gets the whole task, plus why an
    earlier attempt failed when there was one."""
    if resume:
        text = message or "Carry on with the build: run the tests and fix what fails."
        text += "\n\nThen run the tests again and rewrite handoff-builder.md."
        return _with_note(text, note)
    text = (
        "Build this run's plan. Read intent.md, plan.md, handoff-planner.md and "
        f"handoff-tester.md in the run folder, {places.run_folder}, and the tester's tests in "
        f"the worktree, {places.worktree}. Make every test pass, then write handoff-builder.md "
        "in the run folder."
    )
    if message:
        text += f"\n\nAn earlier attempt failed:\n\n{message}"
    return _with_note(text, note)


def locked_conflicts(run_folder: Path, files: Sequence[str]) -> tuple[str, ...]:
    """The conflicted `files` that the test lock holds: the builder may not resolve those."""
    locked = load_tester_files(run_folder)
    return tuple(name for name in files if name in locked)


def locked_conflict_reason(files: Sequence[str]) -> str:
    return (
        f"the rebase onto main conflicts in {', '.join(files)}, written by the tester and locked "
        "from the builder, so the builder can't resolve it. Resolve those tests yourself, or "
        "send the run back to the tester."
    )


def lock_rebased(files: Sequence[str]) -> GateResult:
    """The event for a test lock that now follows main's changes to the tester's files."""
    return GateResult(
        gate=TEST_LOCK_GATE,
        passed=True,
        reason=f"the engine's rebase onto main changed {', '.join(files)}, written by the tester; "
        "the test lock now holds their rebased versions.",
    )


def conflict_message(places: Places, files: Sequence[str]) -> str:
    """What the builder is told when a rebase onto main stops on conflicts in `files`."""
    listed = "\n".join(f"- {name}" for name in files)
    return (
        "The engine is rebasing the run branch onto the latest main, and the rebase stopped on "
        f"conflicts in these files of the worktree, {places.worktree}:\n\n{listed}\n\n"
        "Resolve each one: keep what main changed and what this run needs, and remove every "
        "conflict marker line (<<<<<<<, |||||||, =======, >>>>>>>). Edit the files only; don't "
        "run git. The engine carries the rebase on when you finish, then runs the tests."
    )


def _review_message(path: Path, review: str) -> str:
    return (
        f"The reviewer requested changes. Fix every Important finding first. Its review, also "
        f"in {path}:\n\n{review}"
    )


def _reviewer_prompt(
    places: Places, diff: _ReviewDiff, reason: str | None, note: str | None
) -> str:
    text = (
        "Review this run's change. Read intent.md, plan.md and the handoffs in the run folder, "
        f"{places.run_folder}. The diff from {diff.base[:12]} to {diff.head[:12]} is in "
        f"{diff.path}; the files are in the worktree, {places.worktree}. Write review.md in "
        "the run folder."
    )
    if reason:
        text += (
            "\n\nYour last review.md was malformed and was set aside. Write it again from the "
            f"review template:\n\n{reason}"
        )
    return _with_note(text, note)
