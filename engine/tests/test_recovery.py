"""Tests for crash recovery (task 2.24): kill a run at each step, then recover it.

A kill is simulated with `Killed`, a BaseException as a crash or Ctrl-C would be, raised right
after `run.json` saves a given step (before the registry hears of it), or by a fake agent in the
middle of its run. A new `Pipeline` is then built from what is on disk, as a restarted engine
would, and `Pipeline.recover` carries the run on. The fixture repo and the fakes are those of
`test_pipeline.py`: real post-stage checks, gates (with a real pytest run) and stage commits.
"""

import asyncio
import contextlib
import os
import shlex
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from factory_engine import events as ev
from factory_engine.agents.fake import FakeAgentRunner, result, system_init
from factory_engine.agents.runner import AgentResult, AgentSpec, SessionRecorder, not_running
from factory_engine.config import Settings, default_settings, merge_settings, parse_overrides
from factory_engine.errors import PipelineError, RunFileError
from factory_engine.events import EventLog, read_events
from factory_engine.gates.post_stage import Baseline, MainState
from factory_engine.git_ops import Git, commit_stage, run_branch, worktree_dir
from factory_engine.paths import run_dir
from factory_engine.pipeline import Hooks, Pipeline, Places
from factory_engine.providers import get_profile
from factory_engine.recovery import (
    CRASH_PROMPT,
    UPDATE_STEP,
    AttemptRecord,
    Recovery,
    RetryNote,
    SavedBaseline,
    Step,
    load_recovery,
    resume_point,
    save_recovery,
)
from factory_engine.registry import Registry, registry_path
from factory_engine.run import ATTEMPT_FILE, Role, Run, RunState, load_run, new_run, save_run
from factory_engine.states import WORKING

START = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
REPO = "demo"
RUN_ID = 1
SLUG = "add-mul"
FACTORY = Path(__file__).resolve().parents[2]
PYTHON = shlex.quote(Path(sys.executable).as_posix())
PARENT_ENV = {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": "test-oauth-token"}

BASE_FILES = {
    "pytest.ini": "[pytest]\npythonpath = src\n",
    ".gitignore": "__pycache__/\n.pytest_cache/\n",
    "src/calc.py": "def add(a, b):\n    return a + b\n",
    "tests/test_calc.py": "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
}
INTENT = "# Intent\n\n## Request\n\nAdd `mul(a, b)` to `src/calc.py`.\n\n## Answers\n\nNone\n"
PLAN = """# Plan: add mul

## Context

`src/calc.py` has `add`; the request asks for `mul`.

## Past runs used

None

## Files

- `src/calc.py`: add `mul`
- `tests/test_mul.py`: tests for `mul` (tester)

## Steps

1. Add `mul(a, b)` to `src/calc.py`.

## Acceptance criteria

1. `mul(2, 3)` returns 6.

## Test cases

- AC1: `test_mul`: `mul(2, 3)` is 6
"""
BAD_PLAN = PLAN.replace(
    "- `src/calc.py`: add `mul`\n- `tests/test_mul.py`: tests for `mul` (tester)\n", "None\n"
)
TESTER_TEST = "def test_mul():\n    from calc import mul\n\n    assert mul(2, 3) == 6\n"
ADD = BASE_FILES["src/calc.py"]
GOOD_BUILD = ADD + "\n\ndef mul(a, b):\n    return a * b\n"
BETTER_BUILD = ADD + '\n\ndef mul(a, b):\n    """The product of a and b."""\n    return a * b\n'
WRONG_BUILD = ADD + "\n\ndef mul(a, b):\n    return a + b\n"
REVIEW_IMPORTANT = """# Review

## Acceptance criteria

- AC1: not met. `mul` has no docstring, which the repo's conventions require.

## Findings

- [Important] `src/calc.py:5`: `mul` has no docstring. Fix: Add one.

## Verdict

Verdict: request changes
"""
REVIEW_APPROVE = """# Review

## Acceptance criteria

- AC1: met. `tests/test_mul.py::test_mul` passes.

## Findings

None

## Verdict

Verdict: approve
"""
REVIEW_MALFORMED = "# Review\n\nLooks good to me.\n"
STEPS = ("started", "agent", "checked", "passed", "committed")


def handoff(text: str) -> str:
    return f"# Handoff\n\n## Files touched\n\n{text}\n\n## Decisions\n\nNone\n"


# ------------------------------------------------------------------ fakes


class Killed(BaseException):
    """The engine process dying: a crash, a restart or Ctrl-C."""


class FakeTime:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def clock(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=max(seconds, 0.0))
        await asyncio.sleep(0)


@dataclass(frozen=True)
class Act:
    """One agent run: the files it writes, then a session start and a result. `killed`: the
    engine dies after the agent wrote its files (after its session started, unless `early`)."""

    role: Role
    worktree: dict[str, str] = field(default_factory=dict)
    docs: dict[str, str] = field(default_factory=dict)  # in the run folder
    outside: dict[Path, str] = field(default_factory=dict)  # absolute paths it mustn't write
    killed: bool = False
    early: bool = False  # killed before its session started


class Agents:
    """An `AgentRunner` that plays one `Act` per agent run, in order, across restarts."""

    def __init__(self, acts: Sequence[Act]) -> None:
        self.acts = list(acts)
        self.specs: list[AgentSpec] = []
        self._fake: FakeAgentRunner | None = None

    async def run(self, spec: AgentSpec, recorder: SessionRecorder) -> AgentResult:
        assert self.acts, f"no act left in the script for the {spec.role}"
        act = self.acts.pop(0)
        assert act.role == spec.role, f"the script expected the {act.role}, not the {spec.role}"
        self.specs.append(spec)
        session = spec.resume or f"{spec.role}-{len(self.specs)}"
        if act.killed and act.early:
            raise Killed
        write(spec.worktree, act.worktree)
        write(spec.run_folder, act.docs)
        for path, text in act.outside.items():
            path.write_text(text, encoding="utf-8")
        if act.killed:
            recorder.record(system_init(session))
            raise Killed
        self._fake = FakeAgentRunner([[system_init(session), result(session, 100, 20)]])
        return await self._fake.run(spec, recorder)

    async def interrupt(self) -> None:
        if self._fake is None:
            raise not_running()
        await self._fake.interrupt()

    async def send(self, text: str) -> None:
        if self._fake is None:
            raise not_running()
        await self._fake.send(text)

    def of(self, role: Role) -> list[AgentSpec]:
        return [spec for spec in self.specs if spec.role == role]


class Index:
    """The registry. With `kill_at`, the engine dies the first time `run.json` holds that step,
    before the registry is updated."""

    def __init__(self, registry: Registry, run_folder: Path) -> None:
        self.registry = registry
        self.run_folder = run_folder
        self.kill_at: str | None = None
        self.kills = 0

    def upsert_run(self, run: Run) -> None:
        if self.kill_at is not None and run.last_completed_step == self.kill_at:
            self.kill_at = None
            self.kills += 1
            raise Killed
        self.registry.upsert_run(run)


class RecordingHooks(Hooks):
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def update(self, pipeline: Pipeline, run: Run) -> Run:
        self.calls.append("update")
        return run

    async def publish(self, pipeline: Pipeline, run: Run) -> Run:
        self.calls.append("publish")
        return run


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))


class World:
    """A repo with its run's worktree on the run branch, the data folder and a run in `state`."""

    def __init__(self, tmp_path: Path, git_env: dict[str, str], state: RunState) -> None:
        self.time = FakeTime(START)
        self.git = Git(git_env)
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        write(self.repo, BASE_FILES)
        self.git.run(["init", "-q", "-b", "main"], self.repo)
        self.git.run(["config", "core.autocrlf", "false"], self.repo)
        self.git.run(["add", "--all"], self.repo)
        self.git.run(["commit", "-q", "-m", "base"], self.repo)
        self.data = tmp_path / "data"
        self.run_folder = run_dir(self.data, REPO, RUN_ID, SLUG)
        self.run_folder.mkdir(parents=True)
        write(self.run_folder, {"intent.md": INTENT})
        self.worktree = worktree_dir(self.data, REPO, RUN_ID, SLUG)
        self.worktree.parent.mkdir(parents=True)
        branch = run_branch(RUN_ID, SLUG)
        self.git.run(["worktree", "add", "-q", "-b", branch, str(self.worktree), "main"], self.repo)
        skills = tmp_path / "skills"
        skills.mkdir()
        run = new_run(
            run_id=RUN_ID,
            slug=SLUG,
            repo=REPO,
            kind="feature",
            provider="subscription",
            isolation="worktree",
            now=START,
        ).model_copy(update={"state": state})
        save_run(run, self.run_folder)
        self.registry = Registry(registry_path(self.data))
        self.registry.upsert_run(run)
        self.index = Index(self.registry, self.run_folder)
        self.events = EventLog(self.run_folder, repo=REPO, run_id=RUN_ID, clock=self.time.clock)
        self.settings: Settings = merge_settings(
            default_settings(sys.platform),
            parse_overrides({"commands": {"test": f"{PYTHON} -m pytest"}}, "test overrides"),
        )
        self.places = Places(
            repo=self.repo,
            data=self.data,
            worktree=self.worktree,
            run_folder=self.run_folder,
            prompts=FACTORY / "prompts",
            skills=skills,
        )

    def pipeline(self, agents: Agents, hooks: Hooks | None = None) -> Pipeline:
        """A new pipeline from what is on disk, as a restarted engine builds it."""
        return Pipeline(
            load_run(self.run_folder),
            places=self.places,
            settings=self.settings,
            profile=get_profile("subscription"),
            parent_env=PARENT_ENV,
            git=self.git,
            runner=agents,
            events=self.events,
            registry=self.index,
            clock=self.time.clock,
            sleep=self.time.sleep,
            hooks=hooks or Hooks(),
            base_ref="main",
        )

    def drive(
        self,
        agents: Agents,
        hooks: Hooks | None = None,
        *,
        kill_at: str | None = None,
        recovering: bool = False,
    ) -> Run:
        """Carry the run on until it waits or stops, recovering it with a new pipeline whenever
        the engine dies (and at once with `recovering`)."""
        self.index.kill_at = kill_at
        while True:
            pipeline = self.pipeline(agents, hooks)
            with contextlib.suppress(Killed):  # the engine died; a new one recovers the run
                asyncio.run(pipeline.recover() if recovering else pipeline.advance())
            run = self.run()
            recovering = run.state in ("pending", *WORKING)
            if not recovering:
                return run

    def run(self) -> Run:
        return load_run(self.run_folder)

    def commits(self) -> list[str]:
        out = self.git.out(["log", "--reverse", "--format=%s", "main..HEAD"], self.worktree)
        return out.splitlines()

    def doc(self, rel: str) -> str:
        return (self.run_folder / rel).read_text(encoding="utf-8")

    def close(self) -> None:
        self.registry.close()


type Maker = Callable[[RunState], World]


@pytest.fixture
def world_at(tmp_path: Path, git_env: dict[str, str]) -> Iterator[Maker]:
    made: list[World] = []

    def make(state: RunState) -> World:
        world = World(tmp_path, git_env, state)
        made.append(world)
        return world

    yield make
    for world in made:
        world.close()


def act_planner(plan: str = PLAN, *, killed: bool = False, early: bool = False) -> Act:
    docs = {"plan.md": plan, "handoff-planner.md": handoff("plan.md")}
    return Act("planner", docs=docs, killed=killed, early=early)


def act_tester(*, killed: bool = False, early: bool = False) -> Act:
    docs = {"handoff-tester.md": handoff("tests/test_mul.py")}
    return Act(
        "tester", worktree={"tests/test_mul.py": TESTER_TEST}, docs=docs, killed=killed, early=early
    )


def act_builder(code: str = GOOD_BUILD, *, killed: bool = False, early: bool = False) -> Act:
    docs = {"handoff-builder.md": handoff("src/calc.py")}
    return Act("builder", worktree={"src/calc.py": code}, docs=docs, killed=killed, early=early)


def act_reviewer(review: str = REVIEW_APPROVE, *, killed: bool = False, early: bool = False) -> Act:
    return Act("reviewer", docs={"review.md": review}, killed=killed, early=early)


def gates(folder: Path) -> list[str]:
    """The stage gates' results in order (not the post-stage checks)."""
    return [
        f"{event.body.gate} {'pass' if event.body.passed else 'fail'}"
        for event in read_events(folder)
        if isinstance(event.body, ev.GateResult) and not event.body.gate.startswith("post_stage.")
    ]


def needs_you(folder: Path) -> list[str]:
    return [e.body.reason for e in read_events(folder) if isinstance(e.body, ev.NeedsYou)]


@dataclass(frozen=True)
class EndState:
    state: RunState
    attempts: dict[Role, int]
    last_step: str | None
    findings: int
    commits: list[str]
    files: dict[str, str]  # worktree files the run wrote
    review: str | None


def end_state(world: World) -> EndState:
    run = world.run()
    names = ("src/calc.py", "tests/test_mul.py")
    files = {
        name: (world.worktree / name).read_text(encoding="utf-8")
        for name in names
        if (world.worktree / name).is_file()
    }
    review = world.run_folder / "review.md"
    return EndState(
        state=run.state,
        attempts=dict(run.attempts),
        last_step=run.last_completed_step,
        findings=run.metrics.important_findings,
        commits=world.commits(),
        files=files,
        review=review.read_text(encoding="utf-8") if review.is_file() else None,
    )


PLANNED = EndState(
    state="awaiting_plan_approval",
    attempts={"planner": 1},
    last_step="planner 1 committed",
    findings=0,
    commits=["factory(run-1): planner attempt 1"],
    files={"src/calc.py": ADD},
    review=None,
)
MERGEABLE = EndState(
    state="awaiting_merge",
    attempts={"tester": 1, "builder": 1, "reviewer": 1},
    last_step="reviewer 1 committed",
    findings=0,
    commits=[
        "factory(run-1): tester attempt 1",
        "factory(run-1): builder attempt 1",
        "factory(run-1): reviewer attempt 1",
    ],
    files={"src/calc.py": GOOD_BUILD, "tests/test_mul.py": TESTER_TEST},
    review=REVIEW_APPROVE,
)


# ------------------------------------------------------------------ attempt.json


def baseline() -> Baseline:
    return Baseline(
        main=MainState(head="refs/heads/main abc", index="f00", files={"a.txt": ("file", 3, 1, 9)}),
        watched={"/data/x": ("dir", 0, 0, 0)},
        run_folder={"intent.md": ("file", 10, 2, 7)},
        base="abc123",
    )


def record(role: Role = "planner", attempt: int = 1, **update: object) -> AttemptRecord:
    made = AttemptRecord(
        role=role,
        attempt=attempt,
        baseline=SavedBaseline.of(baseline()),
        prompt="Plan this run.",
        session_before=None,
        resumed=False,
    )
    return made.model_copy(update=update)


def test_a_saved_baseline_restores_the_same_baseline() -> None:
    assert SavedBaseline.of(baseline()).restore() == baseline()


def test_attempt_json_round_trips(tmp_path: Path) -> None:
    saved = Recovery(attempt=record(), retry=RetryNote(role="planner", reason="no files"))

    save_recovery(tmp_path, saved)

    assert load_recovery(tmp_path) == saved


def test_no_attempt_json_is_an_empty_recovery(tmp_path: Path) -> None:
    assert load_recovery(tmp_path) == Recovery()


def test_an_invalid_attempt_json_is_refused(tmp_path: Path) -> None:
    (tmp_path / ATTEMPT_FILE).write_text('{"attempt": 3}', encoding="utf-8")

    with pytest.raises(RunFileError, match="attempt record"):
        load_recovery(tmp_path)


@pytest.mark.parametrize(
    ("resumed", "before", "now", "own"),
    [
        (False, None, None, None),  # the agent never got a session
        (False, None, "planner-2", "planner-2"),  # its new session
        (False, "planner-1", "planner-1", None),  # still the last attempt's session
        (False, "planner-1", "planner-2", "planner-2"),
        (True, "builder-1", "builder-1", "builder-1"),  # it resumed the role's session
    ],
)
def test_an_attempts_own_session(
    resumed: bool, before: str | None, now: str | None, own: str | None
) -> None:
    run = new_run(
        run_id=1, slug="s", repo="r", kind="feature", provider="p", isolation="worktree", now=START
    )
    if now is not None:
        run = run.model_copy(update={"session_ids": {"planner": now}})

    made = record(resumed=resumed, session_before=before)

    assert made.own_session(run) == own


def test_steps_parse_back() -> None:
    step = Step("builder", 2, "passed")
    assert Step.parse(step.label) == step
    for label in (None, "", UPDATE_STEP, "builder two passed", "boss 1 passed", "builder 01 x"):
        assert Step.parse(label) is None


def at(state: RunState, step: str | None) -> Run:
    run = new_run(
        run_id=1, slug="s", repo="r", kind="feature", provider="p", isolation="worktree", now=START
    )
    return run.model_copy(update={"state": state, "last_completed_step": step})


@pytest.mark.parametrize(
    ("state", "step", "saved", "agent_started", "action"),
    [
        ("pending", None, None, False, "advance"),
        ("planning", None, None, False, "advance"),
        ("planning", "planner 1 started", record(), False, "carry_on"),
        ("planning", "planner 1 started", None, False, "redo"),  # killed before its agent
        ("planning", "planner 2 started", record(), False, "redo"),  # the record is attempt 1's
        ("planning", "planner 1 started", None, True, "advance"),  # an older run: no record
        ("planning", "planner 1 agent", record(), False, "check"),
        ("planning", "planner 1 agent", None, True, "advance"),
        ("planning", "planner 1 checked", record(), False, "gate"),
        ("planning", "planner 1 passed", record(), False, "commit"),
        ("planning", "planner 1 failed", record(), False, "advance"),
        ("planning", "planner 1 committed", record(), False, "advance"),  # sent back
        ("testing", "planner 1 committed", record(), False, "advance"),  # plan approved
        ("testing", "tester 1 passed", record("tester"), False, "commit"),
        ("building", "builder 1 committed", record("builder"), False, "advance"),  # a conflict
        ("building", "reviewer 1 failed", record("reviewer"), False, "advance"),  # sent back
        ("updating", "builder 1 committed", record("builder"), False, "advance"),
        ("updating", UPDATE_STEP, record("builder"), False, "review"),
        ("reviewing", UPDATE_STEP, record("builder"), False, "advance"),
        ("reviewing", "reviewer 1 committed", record("reviewer"), False, "publish"),
    ],
)
def test_where_an_interrupted_run_carries_on(
    state: RunState,
    step: str | None,
    saved: AttemptRecord | None,
    agent_started: bool,
    action: str,
) -> None:
    point = resume_point(at(state, step), Recovery(attempt=saved), agent_started=agent_started)

    assert point.action == action
    if action in ("carry_on", "check", "gate"):
        assert point.record == saved


@pytest.mark.parametrize(
    ("step", "saved", "retry", "tries"),
    [
        ("reviewer 2 started", record("reviewer", 2, review_try=2), None, 1),
        ("reviewer 2 checked", record("reviewer", 2, review_try=2), None, 1),
        ("reviewer 2 failed", record("reviewer", 2, review_try=2), "reviewer", 2),
        ("reviewer 3 started", record("reviewer", 2, review_try=2), "reviewer", 2),  # redo
        ("reviewer 3 started", record("builder", 2), "builder", 0),  # a new round
    ],
)
def test_the_reviewers_tries_in_its_round_are_kept(
    step: str, saved: AttemptRecord, retry: Role | None, tries: int
) -> None:
    note = RetryNote(role=retry, reason="malformed") if retry is not None else None

    point = resume_point(
        at("reviewing", step), Recovery(attempt=saved, retry=note), agent_started=False
    )

    assert point.review_tries == tries


@pytest.mark.parametrize("state", ["awaiting_plan_approval", "needs_you", "paused", "merged"])
def test_a_run_that_isnt_interrupted_has_no_resume_point(state: RunState) -> None:
    with pytest.raises(PipelineError, match="nothing to recover"):
        resume_point(at(state, None), Recovery(), agent_started=False)


# ------------------------------------------------------------------ kill and resume


def planner_steps() -> list[str]:
    return [f"planner 1 {name}" for name in STEPS]


def later_steps() -> list[str]:
    steps = [f"{role} 1 {name}" for role in ("tester", "builder") for name in STEPS]
    return [*steps, UPDATE_STEP, *(f"reviewer 1 {name}" for name in STEPS)]


def test_an_uninterrupted_plan_ends_as_expected(world_at: Maker) -> None:
    world = world_at("pending")
    world.drive(Agents([act_planner()]))
    assert end_state(world) == PLANNED


def test_an_uninterrupted_run_ends_as_expected(world_at: Maker) -> None:
    world = world_at("testing")
    world.drive(Agents([act_tester(), act_builder(), act_reviewer()]))
    assert end_state(world) == MERGEABLE


@pytest.mark.parametrize("step", planner_steps())
def test_a_planner_killed_after_any_step_resumes_to_the_same_end(
    world_at: Maker, step: str
) -> None:
    world = world_at("pending")
    agents = Agents([act_planner()])

    world.drive(agents, kill_at=step)

    assert world.index.kills == 1
    assert end_state(world) == PLANNED
    assert len(agents.specs) == 1  # no stage reran
    assert gates(world.run_folder) == ["plan pass"]


@pytest.mark.parametrize("step", later_steps())
def test_a_run_killed_after_any_step_resumes_to_the_same_end(world_at: Maker, step: str) -> None:
    world = world_at("testing")
    hooks = RecordingHooks()
    agents = Agents([act_tester(), act_builder(), act_reviewer()])

    world.drive(agents, hooks, kill_at=step)

    assert world.index.kills == 1
    assert end_state(world) == MERGEABLE
    # No stage that passed runs again: one agent run, one passing gate, one update and publish.
    assert [spec.role for spec in agents.specs] == ["tester", "builder", "reviewer"]
    assert gates(world.run_folder) == ["tests pass", "build pass", "review pass"]
    assert hooks.calls == ["update", "publish"]


@pytest.mark.parametrize("role", ["tester", "builder", "reviewer"])
def test_an_agent_killed_mid_run_carries_on_in_its_own_session(world_at: Maker, role: Role) -> None:
    world = world_at("testing")
    acts = {"tester": act_tester, "builder": act_builder, "reviewer": act_reviewer}
    script = [acts[name](killed=name == role) for name in ("tester", "builder", "reviewer")]
    script.insert(script.index(next(a for a in script if a.killed)) + 1, acts[role]())
    agents = Agents(script)

    world.drive(agents)

    assert end_state(world) == MERGEABLE
    killed, carried = agents.of(role)
    assert killed.resume is None
    # The same attempt carries on in the session the killed agent had, told what happened.
    assert (carried.attempt, carried.resume) == (1, f"{role}-{agents.specs.index(killed) + 1}")
    assert carried.prompt.startswith(CRASH_PROMPT) and killed.prompt in carried.prompt


def test_a_planner_killed_mid_run_carries_on_with_its_note(world_at: Maker) -> None:
    world = world_at("pending")
    agents = Agents([act_planner(killed=True), act_planner()])
    with pytest.raises(Killed):
        asyncio.run(world.pipeline(agents).advance())

    done = asyncio.run(world.pipeline(agents).recover(note="Keep it short."))

    assert done.stop == "plan_approval"
    assert end_state(world) == PLANNED
    _, carried = agents.specs
    assert (carried.attempt, carried.resume) == (1, "planner-1")
    assert "Keep it short." in carried.prompt


def test_an_agent_killed_before_its_session_started_runs_again_as_the_same_attempt(
    world_at: Maker,
) -> None:
    world = world_at("pending")
    agents = Agents([act_planner(killed=True, early=True), act_planner()])

    world.drive(agents)

    assert end_state(world) == PLANNED
    first, again = agents.specs
    assert (again.attempt, again.resume, again.prompt) == (1, None, first.prompt)


def test_a_killed_agent_that_wrote_outside_the_worktree_isnt_carried_on(
    world_at: Maker,
) -> None:
    world = world_at("pending")
    planted = world.repo / "planted.txt"
    agents = Agents([Act("planner", docs={"plan.md": PLAN}, outside={planted: "x"}, killed=True)])

    run = world.drive(agents)

    # The check against the killed attempt's saved baseline catches it; no agent carries on.
    assert run.state == "needs_you"
    assert len(agents.specs) == 1
    assert "main checkout" in needs_you(world.run_folder)[-1]
    assert "planted.txt" in needs_you(world.run_folder)[-1]


def test_a_stage_commit_made_before_the_kill_isnt_made_twice(world_at: Maker) -> None:
    world = world_at("testing")
    agents = Agents([act_tester(), act_builder(), act_reviewer()])
    world.index.kill_at = "tester 1 passed"
    with pytest.raises(Killed):
        asyncio.run(world.pipeline(agents).advance())
    # The engine died after the commit, before it saved the step.
    commit_stage(world.git, world.worktree, run_id=RUN_ID, slug=SLUG, role="tester", attempt=1)

    world.drive(agents, recovering=True)

    assert end_state(world) == MERGEABLE


# ------------------------------------------------------------------ retries across a kill


def test_a_planner_retry_after_a_kill_is_told_why_the_last_plan_failed(world_at: Maker) -> None:
    world = world_at("pending")
    agents = Agents([act_planner(BAD_PLAN), act_planner()])

    world.drive(agents, kill_at="planner 1 failed")

    run = world.run()
    assert (run.state, run.attempts) == ("awaiting_plan_approval", {"planner": 2})
    _, retry = agents.specs
    assert retry.resume is None and "failed the planner gate" in retry.prompt
    assert "attempts/plan-1.md" in retry.prompt.replace("\\", "/")


def test_a_builder_retry_after_a_kill_resumes_with_the_test_output(world_at: Maker) -> None:
    world = world_at("testing")
    agents = Agents([act_tester(), act_builder(WRONG_BUILD), act_builder(), act_reviewer()])

    world.drive(agents, kill_at="builder 1 failed")

    run = world.run()
    assert (run.state, run.attempts["builder"]) == ("awaiting_merge", 2)
    first, retry = agents.of("builder")
    assert retry.resume == f"builder-{agents.specs.index(first) + 1}"
    assert "The end of the test output" in retry.prompt
    assert world.doc("attempts/handoff-builder-1.md") == handoff("src/calc.py")


def test_a_review_sent_back_before_a_kill_reaches_the_builder_once(world_at: Maker) -> None:
    world = world_at("testing")
    agents = Agents(
        [
            act_tester(),
            act_builder(),
            act_reviewer(REVIEW_IMPORTANT),
            act_builder(BETTER_BUILD),
            act_reviewer(),
        ]
    )

    world.drive(agents, kill_at="reviewer 1 failed")

    run = world.run()
    assert run.state == "awaiting_merge"
    assert run.metrics.important_findings == 1  # counted once
    _, second = agents.of("builder")
    assert "Important" in second.prompt and "src/calc.py:5" in second.prompt


def test_the_reviewers_tries_are_kept_across_a_kill(world_at: Maker) -> None:
    world = world_at("testing")
    malformed = [act_reviewer(REVIEW_MALFORMED) for _ in range(3)]
    agents = Agents([act_tester(), act_builder(), *malformed])

    run = world.drive(agents, kill_at="reviewer 2 failed")

    # Three tries in the round, not three more after the restart.
    assert run.state == "needs_you"
    assert run.attempts["reviewer"] == 3
    assert "malformed review.md 3 times" in needs_you(world.run_folder)[-1]
