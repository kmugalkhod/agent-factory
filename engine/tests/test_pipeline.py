"""Tests for the stage pipeline (task 2.20): scripted fake agents on a fixture repo.

Each fake agent writes real files into the worktree and the run folder, then replays a short
script of SDK messages, so the real post-stage check, gates (with a real pytest run) and stage
commits run. The repo is a temporary git repo with the run's worktree on its run branch; time
is a fake clock that moves only when something sleeps.
"""

import asyncio
import os
import shlex
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from claude_agent_sdk import CLIConnectionError

from factory_engine import events as ev
from factory_engine.agents import fake
from factory_engine.agents.fake import HOLD, FakeAgentRunner, rate_limit, result, system_init
from factory_engine.agents.runner import AgentResult, AgentSpec, SessionRecorder, not_running
from factory_engine.agents.watchdog import RESUME_PROMPT
from factory_engine.config import Settings, default_settings, merge_settings, parse_overrides
from factory_engine.errors import AgentError
from factory_engine.events import EventLog, read_events
from factory_engine.gates.plan import check_plan_text
from factory_engine.git_ops import Git, run_branch, worktree_dir
from factory_engine.paths import run_dir
from factory_engine.pipeline import (
    UPDATE_STEP,
    Hooks,
    Pipeline,
    PipelineResult,
    Places,
    Step,
)
from factory_engine.providers import get_profile
from factory_engine.registry import Registry, registry_path
from factory_engine.run import Role, Run, RunState, load_run, new_run, save_run
from factory_engine.states import move

START = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
REPO = "demo"
RUN_ID = 1
SLUG = "add-mul"
FACTORY = Path(__file__).resolve().parents[2]
PYTHON = shlex.quote(Path(sys.executable).as_posix())
PYTEST = f"{PYTHON} -m pytest"
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
QUESTIONS = (
    "\n## Questions\n\n1. Should `mul` take floats? Why it matters: the tests differ. "
    "Default if unanswered: yes.\n"
)
SPLIT = "\n## Split\n\n1. Mul: add `mul`, depends on none\n2. Div: add `div`, depends on part 1\n"

# The tester's test: fails until `mul` is built. The import is inside the test, as the tester
# prompt asks, so a missing `mul` fails the test instead of breaking its collection.
TESTER_TEST = "def test_mul():\n    from calc import mul\n\n    assert mul(2, 3) == 6\n"
PASSING_TEST = "def test_mul():\n    assert 2 * 3 == 6\n"
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


def handoff(text: str) -> str:
    return f"# Handoff\n\n## Files touched\n\n{text}\n\n## Decisions\n\nNone\n"


# ------------------------------------------------------------------ fakes


class FakeTime:
    """A clock that moves only when something sleeps on it."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def clock(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=max(seconds, 0.0))
        await asyncio.sleep(0)


@dataclass(frozen=True)
class Act:
    """One agent run: the files it writes, then its messages (a session start, any `extra`
    messages, a result)."""

    role: Role
    worktree: dict[str, str] = field(default_factory=dict)
    docs: dict[str, str] = field(default_factory=dict)  # in the run folder
    extra: tuple[fake.Step, ...] = ()
    ends: bool = True  # False: no closing result (the `extra` messages end the turn)
    outside: dict[Path, str] = field(default_factory=dict)  # absolute paths an agent mustn't write
    error: Exception | None = None  # the runner raises this after the session starts


class Agents:
    """An `AgentRunner` that plays one `Act` per agent run, in order."""

    def __init__(self, acts: Sequence[Act]) -> None:
        self.acts = list(acts)
        self.specs: list[AgentSpec] = []
        self.sessions: list[str] = []
        self._fake: FakeAgentRunner | None = None

    async def run(self, spec: AgentSpec, recorder: SessionRecorder) -> AgentResult:
        assert self.acts, f"no act left in the script for the {spec.role}"
        act = self.acts.pop(0)
        assert act.role == spec.role, f"the script expected the {act.role}, not the {spec.role}"
        self.specs.append(spec)
        write(spec.worktree, act.worktree)
        write(spec.run_folder, act.docs)
        for path, text in act.outside.items():
            path.write_text(text, encoding="utf-8")
        session = spec.resume or f"{spec.role}-{len(self.specs)}"
        self.sessions.append(session)
        if act.error is not None:
            recorder.record(system_init(session))
            raise act.error
        ending = [result(session, 100, 20)] if act.ends else []
        self._fake = FakeAgentRunner([[system_init(session), *act.extra, *ending]])
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
    """The registry, also recording each saved run's last completed step, and the step its
    `run.json` held at that moment."""

    def __init__(self, registry: Registry, run_folder: Path) -> None:
        self.registry = registry
        self.run_folder = run_folder
        self.steps: list[str | None] = []
        self.on_disk: list[str | None] = []

    def upsert_run(self, run: Run) -> None:
        self.registry.upsert_run(run)
        self.steps.append(run.last_completed_step)
        self.on_disk.append(load_run(self.run_folder).last_completed_step)


class RecordingHooks(Hooks):
    def __init__(self) -> None:
        self.calls: list[tuple[str, RunState]] = []

    async def update(self, pipeline: Pipeline, run: Run) -> Run:
        self.calls.append(("update", run.state))
        return run

    async def publish(self, pipeline: Pipeline, run: Run) -> Run:
        self.calls.append(("publish", run.state))
        return run


class StoppingHooks(Hooks):
    """Hooks that stop the run in `stop_in` (`update` or `publish`) and return the moved run
    without saving it: the pipeline must save it."""

    def __init__(self, stop_in: str) -> None:
        self.stop_in = stop_in

    def _stop(self, pipeline: Pipeline, run: Run) -> Run:
        moved = move(run, "needs_you", log=pipeline.events)
        pipeline.events.append(ev.NeedsYou(reason=f"the {self.stop_in} hook stopped"))
        return moved

    async def update(self, pipeline: Pipeline, run: Run) -> Run:
        return self._stop(pipeline, run) if self.stop_in == "update" else run

    async def publish(self, pipeline: Pipeline, run: Run) -> Run:
        return self._stop(pipeline, run) if self.stop_in == "publish" else run


class ConflictHooks(Hooks):
    """An update that sends the builder back once (as a rebase conflict would), then passes."""

    def __init__(self) -> None:
        self.updates = 0

    async def update(self, pipeline: Pipeline, run: Run) -> Run:
        self.updates += 1
        if self.updates > 1:
            return run
        pipeline.tell_builder("rebase conflict in src/calc.py: resolve it.")
        return move(run, "building", log=pipeline.events)


# ------------------------------------------------------------------ the fixture repo


def write(root: Path, files: dict[str, str]) -> None:
    """Write each file's bytes exactly (`\\n` stays LF on every platform)."""
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))


class World:
    """A repo with a base commit on `main`, the run's worktree on its run branch, the data
    folder with the run folder and registry, and a run starting in `state`."""

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
        self.skills = tmp_path / "skills"  # no factory skills: nothing to copy per agent
        self.skills.mkdir()
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
            parse_overrides({"commands": {"test": PYTEST}}, "test overrides"),
        )
        self.places = Places(
            repo=self.repo,
            data=self.data,
            worktree=self.worktree,
            run_folder=self.run_folder,
            prompts=FACTORY / "prompts",
            skills=self.skills,
        )

    def pipeline(self, agents: Agents, hooks: Hooks | None = None) -> Pipeline:
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

    def advance(self, agents: Agents, hooks: Hooks | None = None) -> PipelineResult:
        return asyncio.run(self.pipeline(agents, hooks).advance())

    def approve_plan(self) -> None:
        """What plan approval (task 2.21) does: move the run on to the tester."""
        run = move(load_run(self.run_folder), "testing", log=self.events)
        save_run(run, self.run_folder)
        self.index.upsert_run(run)

    def run(self) -> Run:
        return load_run(self.run_folder)

    def commits(self) -> list[str]:
        """The run branch's commit messages since `main`, oldest first."""
        out = self.git.out(["log", "--reverse", "--format=%s", "main..HEAD"], self.worktree)
        return out.splitlines()

    def doc(self, rel: str) -> str:
        return (self.run_folder / rel).read_text(encoding="utf-8")

    def close(self) -> None:
        self.registry.close()


type Maker = Callable[[RunState], World]


@pytest.fixture
def world_at(tmp_path: Path, git_env: dict[str, str]) -> Iterator[Maker]:
    """Makes the test's world with its run in a given state; closes the registry after."""
    made: list[World] = []

    def make(state: RunState) -> World:
        world = World(tmp_path, git_env, state)
        made.append(world)
        return world

    yield make
    for world in made:
        world.close()


def summary(folder: Path) -> list[str]:
    """The run's events that tell its story: states, stages, gates (not each post-stage check)
    and stops."""
    out: list[str] = []
    for event in read_events(folder):
        body = event.body
        if isinstance(body, ev.StateChanged):
            out.append(f"state {body.from_state}>{body.to_state}")
        elif isinstance(body, ev.StageStarted):
            out.append(f"start {body.stage} {body.attempt}")
        elif isinstance(body, ev.StageFinished):
            out.append(f"finish {body.stage} {body.attempt} {body.outcome}")
        elif isinstance(body, ev.GateResult) and not body.gate.startswith("post_stage."):
            out.append(f"gate {body.gate} {'pass' if body.passed else 'fail'}")
        elif isinstance(body, ev.NeedsYou):
            out.append("needs_you")
        elif isinstance(body, ev.Paused):
            out.append(f"paused {body.reason}")
    return out


def post_stage_results(folder: Path) -> list[tuple[str, bool]]:
    return [
        (event.body.gate, event.body.passed)
        for event in read_events(folder)
        if isinstance(event.body, ev.GateResult) and event.body.gate.startswith("post_stage.")
    ]


def deduped(steps: Sequence[str | None]) -> list[str | None]:
    out: list[str | None] = []
    for step in steps:
        if not out or out[-1] != step:
            out.append(step)
    return out


def stage(role: Role, attempt: int, outcome: str) -> list[str]:
    return [f"start {role} {attempt}", f"finish {role} {attempt} completed", outcome]


def planner(plan: str = PLAN) -> Act:
    return Act("planner", docs={"plan.md": plan, "handoff-planner.md": handoff("plan.md")})


def tester(test: str = TESTER_TEST) -> Act:
    return Act(
        "tester",
        worktree={"tests/test_mul.py": test},
        docs={"handoff-tester.md": handoff("tests/test_mul.py")},
    )


def builder(code: str = GOOD_BUILD, note: str = "src/calc.py") -> Act:
    return Act(
        "builder",
        worktree={"src/calc.py": code},
        docs={"handoff-builder.md": handoff(note)},
    )


def reviewer(review: str = REVIEW_APPROVE) -> Act:
    return Act("reviewer", docs={"review.md": review})


# ------------------------------------------------------------------ the happy path


def test_happy_path_runs_every_stage_to_awaiting_merge(world_at: Maker) -> None:
    world = world_at("pending")
    agents = Agents([planner(), tester(), builder(), reviewer()])

    first = world.advance(agents)

    assert (first.stop, first.run.state) == ("plan_approval", "awaiting_plan_approval")
    assert first.plan is not None and first.plan.passed
    assert world.commits() == ["factory(run-1): planner attempt 1"]

    world.approve_plan()
    second = world.advance(agents)

    assert (second.stop, second.run.state) == ("awaiting_merge", "awaiting_merge")
    assert summary(world.run_folder) == [
        "state pending>planning",
        *stage("planner", 1, "gate plan pass"),
        "state planning>awaiting_plan_approval",
        "state awaiting_plan_approval>testing",
        *stage("tester", 1, "gate tests pass"),
        "state testing>building",
        *stage("builder", 1, "gate build pass"),
        "state building>updating",
        "state updating>reviewing",
        *stage("reviewer", 1, "gate review pass"),
        "state reviewing>awaiting_merge",
    ]
    checks = post_stage_results(world.run_folder)
    assert len(checks) == 4 * 4 and all(passed for _, passed in checks)
    assert world.commits() == [
        "factory(run-1): planner attempt 1",
        "factory(run-1): tester attempt 1",
        "factory(run-1): builder attempt 1",
        "factory(run-1): reviewer attempt 1",
    ]
    run = world.run()
    assert run == second.run
    assert run.attempts == {"planner": 1, "tester": 1, "builder": 1, "reviewer": 1}
    assert run.session_ids == {
        "planner": "planner-1",
        "tester": "tester-2",
        "builder": "builder-3",
        "reviewer": "reviewer-4",
    }
    assert run.last_completed_step == "reviewer 1 committed"
    assert world.registry.get_run(REPO, RUN_ID) == run
    assert not agents.acts
    assert [spec.resume for spec in agents.specs] == [None, None, None, None]
    assert (world.worktree / "src/calc.py").read_text(encoding="utf-8") == GOOD_BUILD


def test_the_last_completed_step_is_saved_to_run_json_after_every_step(
    world_at: Maker,
) -> None:
    world = world_at("pending")
    agents = Agents([planner(), tester(), builder(), reviewer()])
    world.advance(agents)
    world.approve_plan()
    world.advance(agents)

    def steps(role: Role) -> list[str | None]:
        names = ("started", "agent", "checked", "passed", "committed")
        return [f"{role} 1 {name}" for name in names]

    assert deduped(world.index.steps) == [
        None,
        *steps("planner"),
        *steps("tester"),
        *steps("builder"),
        UPDATE_STEP,
        *steps("reviewer"),
    ]
    assert world.index.on_disk == world.index.steps  # run.json held it at every save


def test_steps_parse_back() -> None:
    step = Step("builder", 2, "passed")
    assert step.label == "builder 2 passed"
    assert Step.parse(step.label) == step
    for label in (None, "", UPDATE_STEP, "builder two passed", "boss 1 passed", "builder 1 x"):
        assert Step.parse(label) is None


# ------------------------------------------------------------------ the planner


def test_a_bad_plan_retries_the_planner_with_the_reason(world_at: Maker) -> None:
    world = world_at("pending")
    agents = Agents([planner(BAD_PLAN), planner()])

    done = world.advance(agents)

    assert (done.stop, done.run.state) == ("plan_approval", "awaiting_plan_approval")
    assert summary(world.run_folder) == [
        "state pending>planning",
        *stage("planner", 1, "gate plan fail"),
        *stage("planner", 2, "gate plan pass"),
        "state planning>awaiting_plan_approval",
    ]
    first, second = agents.of("planner")
    assert second.resume is None  # a retry is a new session
    assert check_plan_text(BAD_PLAN).reason in second.prompt
    assert world.commits() == ["factory(run-1): planner attempt 2"]
    assert done.run.attempts == {"planner": 2}
    assert first.prompt != second.prompt


def test_the_planner_cap_stops_the_run_for_you(world_at: Maker) -> None:
    world = world_at("pending")
    agents = Agents([planner(BAD_PLAN), planner(BAD_PLAN), planner(BAD_PLAN)])

    done = world.advance(agents)

    assert (done.stop, done.run.state) == ("needs_you", "needs_you")
    events = summary(world.run_folder)
    assert events[-3:] == ["gate plan fail", "state planning>needs_you", "needs_you"]
    assert events.count("gate plan fail") == 3
    [reason] = [
        event.body.reason
        for event in read_events(world.run_folder)
        if isinstance(event.body, ev.NeedsYou)
    ]
    assert "cap of 3" in reason
    assert world.commits() == []


@pytest.mark.parametrize(("extra", "stop"), [(QUESTIONS, "questions"), (SPLIT, "split")])
def test_questions_and_splits_wait_for_your_decision(
    world_at: Maker, extra: str, stop: str
) -> None:
    world = world_at("pending")
    done = world.advance(Agents([planner(PLAN + extra)]))

    assert (done.stop, done.run.state) == (stop, "awaiting_plan_approval")
    assert done.plan is not None
    assert done.plan.questions if stop == "questions" else done.plan.split
    # Called again while it waits, the pipeline runs nothing and says the same.
    again = world.advance(Agents([]))
    assert (again.stop, again.run) == (stop, done.run)


def test_a_usage_limit_pauses_and_resumes_the_same_session(world_at: Maker) -> None:
    world = world_at("pending")
    reset = START + timedelta(hours=1)
    limited = Act("planner", extra=(rate_limit(int(reset.timestamp())),))
    agents = Agents([limited, planner()])

    done = world.advance(agents)

    assert (done.stop, done.run.state) == ("plan_approval", "awaiting_plan_approval")
    assert summary(world.run_folder) == [
        "state pending>planning",
        "start planner 1",
        "state planning>paused",
        "paused usage_limit",
        "state paused>planning",
        "finish planner 1 completed",
        "gate plan pass",
        "state planning>awaiting_plan_approval",
    ]
    first, resumed = agents.of("planner")
    assert (resumed.resume, resumed.attempt, resumed.prompt) == (
        agents.sessions[0],
        1,
        RESUME_PROMPT,
    )
    assert world.time.now >= reset
    assert done.run.pause is None
    assert done.run.attempts == {"planner": 1}
    assert first.attempt == 1


# ------------------------------------------------------------------ the tester


def test_tests_that_pass_before_the_build_retry_the_tester(world_at: Maker) -> None:
    world = world_at("testing")
    agents = Agents([tester(PASSING_TEST), tester(), builder(), reviewer()])

    done = world.advance(agents)

    assert (done.stop, done.run.state) == ("awaiting_merge", "awaiting_merge")
    assert summary(world.run_folder)[:7] == [
        *stage("tester", 1, "gate tests fail"),
        *stage("tester", 2, "gate tests pass"),
        "state testing>building",
    ]
    _, second = agents.of("tester")
    assert second.resume is None
    assert "tests pass before the build" in second.prompt
    assert world.commits()[0] == "factory(run-1): tester attempt 2"


# ------------------------------------------------------------------ the builder


def test_a_builder_failing_three_times_needs_you_with_its_handoffs_kept(
    world_at: Maker,
) -> None:
    world = world_at("testing")
    agents = Agents(
        [
            tester(),
            builder(WRONG_BUILD, "attempt 1"),
            builder(WRONG_BUILD, "attempt 2"),
            builder(WRONG_BUILD, "attempt 3"),
        ]
    )

    done = world.advance(agents)

    assert (done.stop, done.run.state) == ("needs_you", "needs_you")
    assert summary(world.run_folder) == [
        *stage("tester", 1, "gate tests pass"),
        "state testing>building",
        *stage("builder", 1, "gate build fail"),
        *stage("builder", 2, "gate build fail"),
        *stage("builder", 3, "gate build fail"),
        "state building>needs_you",
        "needs_you",
    ]
    assert world.doc("attempts/handoff-builder-1.md") == handoff("attempt 1")
    assert world.doc("attempts/handoff-builder-2.md") == handoff("attempt 2")
    assert world.doc("handoff-builder.md") == handoff("attempt 3")
    assert not (world.run_folder / "attempts" / "handoff-builder-3.md").exists()
    first, second, third = agents.of("builder")
    session = agents.sessions[1]
    assert (first.resume, second.resume, third.resume) == (None, session, session)
    assert second.prompt.startswith("tests fail")
    assert "test_mul" in third.prompt
    assert done.run.attempts["builder"] == 3
    assert world.commits() == ["factory(run-1): tester attempt 1"]


def test_a_post_stage_failure_needs_you_without_a_retry(world_at: Maker) -> None:
    world = world_at("testing")
    # The builder rewrites the tester's locked test: the hooks would block it; the engine's
    # check after the stage catches it.
    cheat = Act(
        "builder",
        worktree={"src/calc.py": WRONG_BUILD, "tests/test_mul.py": PASSING_TEST},
        docs={"handoff-builder.md": handoff("src/calc.py")},
    )
    agents = Agents([tester(), cheat])

    done = world.advance(agents)

    assert (done.stop, done.run.state) == ("needs_you", "needs_you")
    assert summary(world.run_folder)[-4:] == [
        "start builder 1",
        "finish builder 1 completed",
        "state building>needs_you",
        "needs_you",
    ]
    assert ("post_stage.allowed_paths", False) in post_stage_results(world.run_folder)
    assert len(agents.of("builder")) == 1
    assert world.commits() == ["factory(run-1): tester attempt 1"]
    assert done.run.last_completed_step == "builder 1 agent"


# ------------------------------------------------------------------ the reviewer


def test_an_important_finding_resumes_the_builder_then_the_review_passes(
    world_at: Maker,
) -> None:
    world = world_at("testing")
    hooks = RecordingHooks()
    agents = Agents(
        [
            tester(),
            builder(GOOD_BUILD, "attempt 1"),
            reviewer(REVIEW_IMPORTANT),
            builder(BETTER_BUILD, "attempt 2"),
            reviewer(REVIEW_APPROVE),
        ]
    )

    done = world.advance(agents, hooks)

    assert (done.stop, done.run.state) == ("awaiting_merge", "awaiting_merge")
    assert summary(world.run_folder) == [
        *stage("tester", 1, "gate tests pass"),
        "state testing>building",
        *stage("builder", 1, "gate build pass"),
        "state building>updating",
        "state updating>reviewing",
        *stage("reviewer", 1, "gate review fail"),
        "state reviewing>building",
        *stage("builder", 2, "gate build pass"),
        "state building>updating",
        "state updating>reviewing",
        *stage("reviewer", 2, "gate review pass"),
        "state reviewing>awaiting_merge",
    ]
    first, second = agents.of("builder")
    assert (first.resume, second.resume) == (None, agents.sessions[1])
    assert "Important" in second.prompt and "src/calc.py:5" in second.prompt
    assert world.doc("attempts/review-1.md") == REVIEW_IMPORTANT
    assert world.doc("review.md") == REVIEW_APPROVE
    assert world.doc("attempts/handoff-builder-1.md") == handoff("attempt 1")
    assert done.run.metrics.important_findings == 1
    assert done.run.attempts == {"tester": 1, "builder": 2, "reviewer": 2}
    assert world.commits() == [
        "factory(run-1): tester attempt 1",
        "factory(run-1): builder attempt 1",
        "factory(run-1): builder attempt 2",
        "factory(run-1): reviewer attempt 2",
    ]
    assert hooks.calls == [
        ("update", "updating"),
        ("update", "updating"),
        ("publish", "reviewing"),
    ]
    # The reviewer is given the diff of the whole run, from main to the run branch's head.
    diff = world.doc("review-diff.patch")
    assert "+def mul(a, b):" in diff and "tests/test_mul.py" in diff


def test_a_malformed_review_retries_the_reviewer_not_the_builder(world_at: Maker) -> None:
    world = world_at("testing")
    agents = Agents([tester(), builder(), reviewer(REVIEW_MALFORMED), reviewer(REVIEW_APPROVE)])

    done = world.advance(agents)

    assert (done.stop, done.run.state) == ("awaiting_merge", "awaiting_merge")
    assert summary(world.run_folder)[-7:] == [
        *stage("reviewer", 1, "gate review fail"),
        *stage("reviewer", 2, "gate review pass"),
        "state reviewing>awaiting_merge",
    ]
    assert len(agents.of("builder")) == 1
    _, second = agents.of("reviewer")
    assert second.resume is None
    assert "malformed" in second.prompt
    assert world.doc("attempts/review-1.md") == REVIEW_MALFORMED
    assert done.run.metrics.important_findings == 0
    assert world.commits()[-1] == "factory(run-1): reviewer attempt 2"


# ------------------------------------------------------------------ stops and failures


def needs_you_reasons(folder: Path) -> list[str]:
    return [
        event.body.reason for event in read_events(folder) if isinstance(event.body, ev.NeedsYou)
    ]


def test_a_stalled_agent_that_wrote_outside_the_worktree_fails_the_check(
    world_at: Maker,
) -> None:
    world = world_at("pending")
    stalled = Act("planner", extra=(HOLD,), ends=False, outside={world.repo / "planted.txt": "x"})

    done = world.advance(Agents([stalled]))

    assert (done.stop, done.run.state) == ("needs_you", "needs_you")
    assert summary(world.run_folder) == [
        "state pending>planning",
        "start planner 1",
        "state planning>needs_you",
        "needs_you",  # the stall
        "finish planner 1 interrupted",
        "needs_you",  # the check after it
    ]
    assert ("post_stage.main_checkout", False) in post_stage_results(world.run_folder)
    reason = needs_you_reasons(world.run_folder)[-1]
    assert "post_stage.main_checkout" in reason and "no tool activity" in reason
    assert world.run() == done.run


@pytest.mark.parametrize("planted", [False, True])
def test_a_turn_cap_pauses_unless_the_check_after_it_fails(world_at: Maker, planted: bool) -> None:
    world = world_at("pending")
    capped = Act(
        "planner",
        extra=(result("planner-1", 10, 5, is_error=True, subtype="error_max_turns"),),
        ends=False,
        outside={world.repo / "planted.txt": "x"} if planted else {},
    )

    done = world.advance(Agents([capped]))

    stopped = [
        "state pending>planning",
        "start planner 1",
        "state planning>paused",
        "paused turn_cap",
        "finish planner 1 failed",
    ]
    checks = post_stage_results(world.run_folder)
    assert len(checks) == 4
    if planted:
        assert (done.stop, done.run.state, done.run.pause) == ("needs_you", "needs_you", None)
        assert summary(world.run_folder) == [*stopped, "state paused>needs_you", "needs_you"]
        assert "turn cap" in needs_you_reasons(world.run_folder)[-1]
    else:
        assert (done.stop, done.run.state) == ("paused", "paused")
        assert done.run.pause is not None and done.run.pause.reason == "turn_cap"
        assert summary(world.run_folder) == stopped
        assert all(passed for _, passed in checks)
    assert world.run() == done.run


def capped_planner() -> Act:
    return Act(
        "planner",
        extra=(result("planner-1", 10, 5, is_error=True, subtype="error_max_turns"),),
        ends=False,
    )


def test_resuming_a_paused_run_carries_on_the_same_attempt_and_session(world_at: Maker) -> None:
    world = world_at("pending")
    caps = world.settings.caps.model_copy(update={"planner_attempts": 1})
    world.settings = world.settings.model_copy(update={"caps": caps})
    paused = world.advance(Agents([capped_planner()]))
    assert (paused.run.state, paused.run.attempts) == ("paused", {"planner": 1})
    seen = len(read_events(world.run_folder))
    agents = Agents([planner()])

    done = asyncio.run(world.pipeline(agents).resume_paused(note="Keep it short."))

    # The planner's only attempt (cap 1) carries on: no new attempt, no cap reached.
    assert (done.stop, done.run.state) == ("plan_approval", "awaiting_plan_approval")
    [spec] = agents.specs
    assert (spec.resume, spec.attempt) == ("planner-1", 1)
    assert spec.prompt.startswith(RESUME_PROMPT)
    assert "Keep it short." in spec.prompt
    assert done.run.attempts == {"planner": 1}
    assert done.run.pause is None
    assert summary(world.run_folder)[-5:] == [
        "state paused>planning",
        "start planner 1",
        "finish planner 1 completed",
        "gate plan pass",
        "state planning>awaiting_plan_approval",
    ]
    assert len(read_events(world.run_folder)) > seen


def test_resume_paused_refuses_a_run_that_isnt_paused(world_at: Maker) -> None:
    world = world_at("planning")

    with pytest.raises(AgentError, match="isn't paused"):
        asyncio.run(world.pipeline(Agents([])).resume_paused())

    assert world.run().state == "planning"


def test_a_runner_error_stops_the_run_for_you_after_the_check(world_at: Maker) -> None:
    world = world_at("pending")
    secret = "zq81Kp0vXr4mWb7"
    dropped = Act(
        "planner",
        docs={"plan.md": PLAN},
        error=CLIConnectionError(f"connection dropped: API_TOKEN={secret}"),
    )

    done = world.advance(Agents([dropped]))

    assert (done.stop, done.run.state) == ("needs_you", "needs_you")
    assert summary(world.run_folder) == [
        "state pending>planning",
        "start planner 1",
        "finish planner 1 failed",
        "state planning>needs_you",
        "needs_you",
    ]
    checks = post_stage_results(world.run_folder)
    assert len(checks) == 4 and all(passed for _, passed in checks)
    reason = needs_you_reasons(world.run_folder)[-1]
    assert "CLIConnectionError" in reason and "connection dropped" in reason
    log = (world.run_folder / "events.jsonl").read_text(encoding="utf-8")
    assert secret not in log
    run = world.run()
    assert run == done.run
    assert run.session_ids == {"planner": "planner-1"}  # kept for a resume
    assert run.last_completed_step == "planner 1 started"
    assert world.commits() == []


def test_an_engine_bug_inside_an_agent_run_is_raised(world_at: Maker) -> None:
    world = world_at("pending")
    with pytest.raises(KeyError, match="oops"):
        world.advance(Agents([Act("planner", error=KeyError("oops"))]))


# ------------------------------------------------------------------ hooks


@pytest.mark.parametrize("stop_in", ["update", "publish"])
def test_a_hook_that_stops_the_run_is_saved(world_at: Maker, stop_in: str) -> None:
    world = world_at("testing")
    acts = [tester(), builder()] + ([reviewer()] if stop_in == "publish" else [])
    agents = Agents(acts)

    done = world.advance(agents, StoppingHooks(stop_in))

    assert (done.stop, done.run.state) == ("needs_you", "needs_you")
    assert not agents.acts
    run = world.run()
    assert run == done.run
    assert world.registry.get_run(REPO, RUN_ID) == run
    assert needs_you_reasons(world.run_folder) == [f"the {stop_in} hook stopped"]


def test_an_update_hook_can_send_the_run_back_to_the_builder(world_at: Maker) -> None:
    world = world_at("testing")
    agents = Agents(
        [
            tester(),
            builder(GOOD_BUILD, "attempt 1"),
            builder(BETTER_BUILD, "attempt 2"),
            reviewer(),
        ]
    )

    done = world.advance(agents, ConflictHooks())

    assert (done.stop, done.run.state) == ("awaiting_merge", "awaiting_merge")
    events = summary(world.run_folder)
    assert events[events.index("state building>updating") + 1] == "state updating>building"
    first, second = agents.of("builder")
    assert second.resume == agents.sessions[1]
    assert second.prompt.startswith("rebase conflict in src/calc.py")
    assert world.doc("attempts/handoff-builder-1.md") == handoff("attempt 1")


# ------------------------------------------------------------------ metrics and report (2.27)

TURN = 120  # tokens of each scripted agent run: result(session, 100, 20)


def _metric_values(run: Run) -> tuple[bool | None, int, int]:
    return (run.metrics.first_pass, run.metrics.builder_retries, run.metrics.important_findings)


@pytest.mark.parametrize(
    ("acts", "state", "expected", "attempts"),
    [
        (
            [tester(), builder(), reviewer()],
            "awaiting_merge",
            (True, 0, 0),
            [("tester", 1), ("builder", 1), ("reviewer", 1)],
        ),
        (
            [tester(), builder(WRONG_BUILD), builder(), reviewer()],
            "awaiting_merge",
            (False, 1, 0),
            [("tester", 1), ("builder", 1), ("builder", 2), ("reviewer", 1)],
        ),
        (
            [tester(), builder(), reviewer(REVIEW_IMPORTANT), builder(BETTER_BUILD), reviewer()],
            "awaiting_merge",
            (False, 1, 1),
            [("tester", 1), ("builder", 1), ("reviewer", 1), ("builder", 2), ("reviewer", 2)],
        ),
        (
            [tester(), builder(), reviewer(REVIEW_MALFORMED), reviewer()],
            "awaiting_merge",
            (True, 0, 0),  # a malformed review decides nothing
            [("tester", 1), ("builder", 1), ("reviewer", 1), ("reviewer", 2)],
        ),
        (
            [tester(), builder(WRONG_BUILD), builder(WRONG_BUILD), builder(WRONG_BUILD)],
            "needs_you",
            (False, 2, 0),
            [("tester", 1), ("builder", 1), ("builder", 2), ("builder", 3)],
        ),
    ],
    ids=["first-pass", "build-retried", "review-sends-back", "malformed-review", "builder-cap"],
)
def test_scripted_runs_record_their_metrics_in_run_json_and_the_registry(
    world_at: Maker,
    acts: list[Act],
    state: RunState,
    expected: tuple[bool | None, int, int],
    attempts: list[tuple[Role, int]],
) -> None:
    world = world_at("testing")

    done = world.advance(Agents(acts))

    run = world.run()
    assert done.run == run and run.state == state
    assert _metric_values(run) == expected
    assert [(use.role, use.attempt) for use in run.metrics.token_use] == attempts
    assert all(use.input + use.output == TURN for use in run.metrics.token_use)
    assert run.metrics.pr_lines_changed_by_you is None  # nothing merged
    assert world.registry.get_run(REPO, RUN_ID) == run


@pytest.mark.parametrize(
    ("acts", "status", "criterion"),
    [
        ([tester(), builder(), reviewer()], "awaiting merge", "- AC1: met, `test_mul`"),
        (
            [tester(), builder(WRONG_BUILD), builder(WRONG_BUILD), builder(WRONG_BUILD)],
            "needs you",
            "- AC1: not reviewed, `test_mul`",
        ),
    ],
    ids=["awaiting-merge", "needs-you"],
)
def test_the_report_is_written_when_the_run_stops(
    world_at: Maker, acts: list[Act], status: str, criterion: str
) -> None:
    world = world_at("testing")
    write(world.run_folder, {"plan.md": PLAN})  # the approved plan a run in testing has
    world.advance(Agents(acts))
    report = world.doc("report.md").splitlines()
    assert report[0] == f"# Report: run-{RUN_ID}-{SLUG}"
    assert f"Status: {status}" in report
    assert len(report) <= 60
    assert criterion in report


def test_no_report_while_the_plan_waits_for_approval(world_at: Maker) -> None:
    world = world_at("pending")
    world.advance(Agents([planner()]))
    assert not (world.run_folder / "report.md").exists()


def test_a_usage_limit_pause_is_counted_after_the_run_resumes(world_at: Maker) -> None:
    world = world_at("pending")
    reset = START + timedelta(hours=1)
    agents = Agents([Act("planner", extra=(rate_limit(int(reset.timestamp())),)), planner()])

    done = world.advance(agents)

    assert done.run.pause is None
    assert [(p.reason, p.role, p.attempt) for p in done.run.metrics.pauses] == [
        ("usage_limit", "planner", 1)
    ]
    assert world.registry.get_run(REPO, RUN_ID) == done.run
