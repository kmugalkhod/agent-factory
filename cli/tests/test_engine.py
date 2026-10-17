"""Tests for the real engine actions behind the `factory` command (task 2.28): they build the
engine's objects (settings, provider profile, intake, pipeline, registry, events) and drive a
run, with a scripted fake planner in place of the real agent runner. A temporary repo with a
bare "remote"; no network and no real model."""

import asyncio
import subprocess
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from factory_cli.engine import GLOBAL_SETTINGS, LOCK_FILE, Deps, Engine
from factory_cli.errors import CliError
from factory_cli.lock import RunLock
from factory_engine.agent_config import agent_config_dir
from factory_engine.agents.fake import FakeAgentRunner, result, system_init
from factory_engine.agents.runner import AgentResult, AgentSpec, SessionRecorder, not_running
from factory_engine.agents.watchdog import RESUME_PROMPT
from factory_engine.config import Settings
from factory_engine.errors import DecisionError, FactoryError, NotReadyError, ProviderError
from factory_engine.events import Event, EventObserver, read_events
from factory_engine.git_ops import worktree_dir
from factory_engine.paths import repo_runs_dir, run_dir
from factory_engine.readiness import Problem, Readiness
from factory_engine.registry import Registry, registry_path
from factory_engine.run import Pause, RunState, load_run, save_run

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
FACTORY = Path(__file__).resolve().parents[2]
TOKEN = "test-oauth-token"

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
QUESTIONS = "\n## Questions\n\n1. Should `mul` take floats? Default if unanswered: yes.\n"


class Planner:
    """An `AgentRunner` where every agent is a planner that writes `plan.md` (one per run, in
    order) and replays a session start and a result."""

    def __init__(self, plans: Sequence[str]) -> None:
        self.plans = list(plans)
        self.specs: list[AgentSpec] = []
        self._fake: FakeAgentRunner | None = None
        self.before: Callable[[], None] | None = None  # runs when a pipeline gets its runner

    def factory(self, parent_env: object) -> "Planner":
        if self.before is not None:
            self.before()
        return self

    async def run(self, spec: AgentSpec, recorder: SessionRecorder) -> AgentResult:
        assert spec.role == "planner", f"only the planner runs here, not the {spec.role}"
        assert self.plans, "no plan left in the script"
        self.specs.append(spec)
        (spec.run_folder / "plan.md").write_text(self.plans.pop(0), encoding="utf-8")
        session = f"planner-{len(self.specs)}"
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


async def _no_sleep(seconds: float) -> None:
    await asyncio.sleep(0)


class Readied:
    """A stand-in for the readiness check: records what it was asked and answers `result`
    (ready by default), so the start tests don't need a GitHub remote or the repo's commands."""

    def __init__(self) -> None:
        self.result = Readiness()
        self.calls: list[tuple[Path, Settings, dict[str, object]]] = []

    def __call__(self, repo: Path, settings: Settings, **kw: object) -> Readiness:
        self.calls.append((repo, settings, kw))
        return self.result


def sh(cwd: Path, *args: str, env: dict[str, str]) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        timeout=60,
        encoding="utf-8",
        errors="replace",
    )
    return done.stdout.strip()


@dataclass
class World:
    repo: Path  # the main checkout, folder name "pilot"
    data: Path
    env: dict[str, str]
    planner: Planner
    readiness: Readied

    def engine(
        self,
        repo: Path | None = None,
        *,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
        platform: str = "win32",
        progress: EventObserver | None = None,
    ) -> Engine:
        deps = Deps(
            env=self.env if env is None else env,
            cwd=cwd or self.repo / "src",  # inside the repo: the repo is found from the folder
            clock=lambda: NOW,
            sleep=_no_sleep,
            platform=platform,
            home=FACTORY,
            runner=self.planner.factory,
            progress=progress,
            readiness=self.readiness,
        )
        return Engine(deps, repo)

    def folder(self, run_id: int = 0, slug: str = "add-mul-to-calc") -> Path:
        return run_dir(self.data, "pilot", run_id, slug)


@pytest.fixture
def world(tmp_path: Path, git_env: dict[str, str]) -> Iterator[World]:
    remote = tmp_path / "remote.git"
    sh(tmp_path, "init", "--bare", "-b", "main", str(remote), env=git_env)
    seed = tmp_path / "seed"
    sh(tmp_path, "init", "-b", "main", str(seed), env=git_env)
    sh(seed, "remote", "add", "origin", str(remote), env=git_env)
    (seed / "src").mkdir()
    (seed / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (seed / "factory.yaml").write_text("commands:\n  test: python -m pytest\n", encoding="utf-8")
    sh(seed, "add", "-A", env=git_env)
    sh(seed, "commit", "-m", "first", env=git_env)
    sh(seed, "push", "origin", "main", env=git_env)
    repo = tmp_path / "pilot"
    sh(tmp_path, "clone", str(remote), str(repo), env=git_env)
    data = tmp_path / "data"
    data.mkdir()
    (data / GLOBAL_SETTINGS).write_text("token_expires: 2027-12-31\n", encoding="utf-8")
    env = {**git_env, "AGENT_FACTORY_DATA": str(data), "CLAUDE_CODE_OAUTH_TOKEN": TOKEN}
    yield World(repo=repo, data=data, env=env, planner=Planner([PLAN]), readiness=Readied())


def set_state(world: World, state: RunState, pause: Pause | None = None) -> None:
    folder = world.folder()
    run = load_run(folder).model_copy(update={"state": state, "pause": pause})
    save_run(run, folder)
    registry = Registry(registry_path(world.data))
    try:
        registry.upsert_run(run)
    finally:
        registry.close()


# ------------------------------------------------------------------ start


def test_start_runs_the_planner_in_the_runs_worktree_and_waits_for_approval(world: World) -> None:
    done = world.engine().start_feature("Add mul to calc", slug=None)

    assert done.stop == "plan_approval"
    assert done.run.state == "awaiting_plan_approval"
    assert done.run_folder == world.folder()
    [spec] = world.planner.specs
    assert spec.worktree == worktree_dir(world.data, "pilot", 0, "add-mul-to-calc")
    assert spec.run_folder == world.folder()
    config = agent_config_dir(world.data, "pilot", 0, "add-mul-to-calc", "planner")
    assert spec.env["CLAUDE_CONFIG_DIR"] == str(config)
    assert spec.env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert "Add mul to calc" in (world.folder() / "intent.md").read_text(encoding="utf-8")
    registry = Registry(registry_path(world.data))
    try:
        [repo] = registry.list_repos()
        assert (repo.name, repo.path) == ("pilot", world.repo.resolve())
        assert registry.get_run("pilot", 0) == load_run(world.folder())
    finally:
        registry.close()
    assert not RunLock(world.folder() / LOCK_FILE).held()  # released once the run waits


def test_start_lists_the_plans_questions(world: World) -> None:
    world.planner.plans = [PLAN + QUESTIONS]

    done = world.engine().start_feature("Add mul to calc", slug="mul")

    assert done.stop == "questions"
    assert len(done.questions) == 1
    assert "Should `mul` take floats?" in done.questions[0]
    assert "yes" in done.questions[0]


def test_a_stop_while_the_run_is_starting_is_refused(world: World) -> None:
    refused: list[str] = []

    def stop_now() -> None:  # after intake published the run, before the pipeline drives it
        with pytest.raises(CliError, match="another factory process") as caught:
            world.engine().stop(0)
        refused.append(str(caught.value))

    world.planner.before = stop_now

    done = world.engine().start_feature("Add mul to calc", slug=None)

    assert refused
    assert done.run.state == "awaiting_plan_approval"
    assert load_run(world.folder()).state == "awaiting_plan_approval"


def test_the_repo_option_beats_the_current_folder(world: World, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    done = world.engine(world.repo, cwd=elsewhere).start_feature("Add mul to calc", slug=None)

    assert done.run.repo == "pilot"


def test_a_folder_outside_any_repo_is_refused(world: World, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    with pytest.raises(CliError, match="--repo"):
        world.engine(cwd=elsewhere).start_feature("Add mul to calc", slug=None)


def test_start_checks_readiness_on_the_main_checkout_before_a_run_is_made(
    world: World,
) -> None:
    done = world.engine().start_feature("Add mul to calc", slug=None)

    [(repo, settings, kw)] = world.readiness.calls
    assert repo == world.repo.resolve()
    assert settings.commands.test == "python -m pytest"  # the repo's factory.yaml
    assert kw["platform"] == "win32"
    assert kw["parent_env"] == world.env
    assert kw["scratch"] == world.data / "readiness"
    assert done.run.state == "awaiting_plan_approval"


def test_a_repo_that_isnt_ready_gets_no_run_and_hears_every_problem(world: World) -> None:
    world.readiness.result = Readiness(
        (
            Problem("claude_md", "`CLAUDE.md` isn't on origin/main. Add a `CLAUDE.md`."),
            Problem("build", "factory.yaml sets no `commands.build`."),
        )
    )

    with pytest.raises(NotReadyError) as caught:
        world.engine().start_feature("Add mul to calc", slug=None)

    assert "1. `CLAUDE.md` isn't on origin/main" in str(caught.value)
    assert "2. factory.yaml sets no `commands.build`" in str(caught.value)
    assert not repo_runs_dir(world.data, "pilot").exists()
    assert world.planner.specs == []
    registry = Registry(registry_path(world.data))
    try:
        assert registry.list_repos() == []  # a refused start registers nothing
    finally:
        registry.close()


def test_start_without_factory_yaml_goes_to_the_readiness_check(
    world: World, git_env: dict[str, str]
) -> None:
    sh(world.repo, "rm", "-q", "factory.yaml", env=git_env)
    world.readiness.result = Readiness((Problem("factory_yaml", "`factory.yaml` is missing."),))

    with pytest.raises(NotReadyError, match=r"factory\.yaml"):
        world.engine().start_feature("Add mul to calc", slug=None)

    [(_repo, settings, _kw)] = world.readiness.calls
    assert settings.commands.test is None  # the built-in defaults: nothing to read
    assert not repo_runs_dir(world.data, "pilot").exists()


def test_start_without_a_token_is_refused_before_a_run_is_made(world: World) -> None:
    env = {k: v for k, v in world.env.items() if k != "CLAUDE_CODE_OAUTH_TOKEN"}

    with pytest.raises(ProviderError, match="CLAUDE_CODE_OAUTH_TOKEN"):
        world.engine(env=env).start_feature("Add mul to calc", slug=None)

    assert not repo_runs_dir(world.data, "pilot").exists()


# ------------------------------------------------------------------ decisions


def test_send_back_reruns_the_planner_with_your_notes(world: World) -> None:
    world.engine().start_feature("Add mul to calc", slug=None)
    world.planner.plans = [PLAN]

    done = world.engine().send_back(0, "Cover zero too.")

    assert done.stop == "plan_approval"
    assert "Cover zero too." in world.planner.specs[-1].prompt


def test_answer_reruns_the_planner_with_your_answers(world: World) -> None:
    world.planner.plans = [PLAN + QUESTIONS, PLAN]
    world.engine().start_feature("Add mul to calc", slug=None)

    done = world.engine().answer(0, ["No, ints only."])

    assert done.stop == "plan_approval"
    assert "No, ints only." in (world.folder() / "intent.md").read_text(encoding="utf-8")


def test_a_refused_decision_changes_nothing(world: World) -> None:
    world.engine().start_feature("Add mul to calc", slug=None)

    with pytest.raises(DecisionError):
        world.engine().send_back(0, "   ")

    assert load_run(world.folder()).state == "awaiting_plan_approval"


def test_approve_on_a_run_awaiting_merge_is_a_merge_approval(world: World) -> None:
    # Here the run has no pull request, so the merge approval refuses and nothing changes.
    world.engine().start_feature("Add mul to calc", slug=None)
    set_state(world, "awaiting_merge")

    with pytest.raises(DecisionError, match="pull request"):
        world.engine().approve(0)

    after = load_run(world.folder())
    assert after.state == "awaiting_merge"
    assert after.merge_approval is None


def test_an_unknown_run_is_an_error(world: World) -> None:
    world.engine().start_feature("Add mul to calc", slug=None)

    with pytest.raises(CliError, match="factory status"):
        world.engine().approve(7)


def test_a_run_another_process_is_driving_is_refused(world: World) -> None:
    world.engine().start_feature("Add mul to calc", slug=None)
    lock = RunLock(world.folder() / LOCK_FILE)
    lock.acquire()
    try:
        with pytest.raises(CliError, match="another factory process"):
            world.engine().approve(0)
        with pytest.raises(CliError, match="another factory process"):
            world.engine().stop(0)
    finally:
        lock.release()
    assert load_run(world.folder()).state == "awaiting_plan_approval"


# ------------------------------------------------------------------ stop and resume


def test_stop_moves_the_run_to_stopped(world: World) -> None:
    world.engine().start_feature("Add mul to calc", slug=None)

    stopped = world.engine().stop(0)

    assert stopped.state == "stopped"
    assert load_run(world.folder()).state == "stopped"
    with pytest.raises(CliError, match="ended"):
        world.engine().stop(0)


def test_resume_carries_an_interrupted_stage_on(world: World) -> None:
    world.engine().start_feature("Add mul to calc", slug=None)
    set_state(world, "planning")  # as a Ctrl-C mid-stage leaves it
    world.planner.plans = [PLAN]

    done = world.engine().resume(0, note="Carry on.")

    assert done.stop == "plan_approval"
    assert "Carry on." in world.planner.specs[-1].prompt


def test_resume_carries_on_a_paused_attempt_in_its_session(world: World) -> None:
    world.engine().start_feature("Add mul to calc", slug=None)
    pause = Pause(
        reason="turn_cap",
        detail="the planner used its 200 turns",
        state="planning",
        role="planner",
        attempt=1,
        session_id="planner-1",
        resume_at=None,
    )
    set_state(world, "paused", pause)
    world.planner.plans = [PLAN]

    done = world.engine().resume(0, note="Keep it short.")

    assert done.stop == "plan_approval"
    spec = world.planner.specs[-1]
    assert (spec.resume, spec.attempt) == ("planner-1", 1)
    assert spec.prompt.startswith(RESUME_PROMPT)
    assert "Keep it short." in spec.prompt
    assert done.run.attempts == {"planner": 1}  # the paused attempt, not a new one
    assert done.run.pause is None


@pytest.mark.parametrize(
    ("state", "match"),
    [
        ("awaiting_plan_approval", "factory approve"),
        ("needs_you", "2.23"),
        ("stopped", "ended"),
    ],
)
def test_resume_refuses_a_run_that_isnt_working(world: World, state: RunState, match: str) -> None:
    world.engine().start_feature("Add mul to calc", slug=None)
    set_state(world, state)

    with pytest.raises(FactoryError, match=match):
        world.engine().resume(0, note=None)


# ------------------------------------------------------------------ status and attach


def test_runs_lists_the_repos_runs_with_attempts_and_tokens(world: World) -> None:
    world.engine().start_feature("Add mul to calc", slug=None)

    [row] = world.engine().runs(all_repos=False)

    assert row.run.state == "awaiting_plan_approval"
    assert row.run.attempts == {"planner": 1}
    assert row.run.metrics.tokens["planner"] > 0
    assert row.running is False
    assert world.engine(cwd=world.data).runs(all_repos=True) == [row]


def test_describe_shows_the_runs_places(world: World) -> None:
    world.engine().start_feature("Add mul to calc", slug=None)

    detail = world.engine().describe(0)

    assert detail.run_folder == world.folder()
    assert detail.worktree == worktree_dir(world.data, "pilot", 0, "add-mul-to-calc")


@pytest.mark.parametrize(
    ("platform", "expected"), [("win32", "$env:CLAUDE_CONFIG_DIR"), ("linux", "env -u")]
)
def test_attach_prints_how_to_resume_the_agents_session(
    world: World, platform: str, expected: str
) -> None:
    world.engine().start_feature("Add mul to calc", slug=None)

    command = world.engine(platform=platform).attach(0, role=None)

    config = agent_config_dir(world.data, "pilot", 0, "add-mul-to-calc", "planner")
    assert "claude --resume planner-1" in command
    assert str(config) in command
    assert str(worktree_dir(world.data, "pilot", 0, "add-mul-to-calc")) in command
    assert expected in command
    assert "ANTHROPIC_API_KEY" in command


def test_attach_needs_a_saved_session(world: World) -> None:
    world.engine().start_feature("Add mul to calc", slug=None)

    with pytest.raises(CliError, match="no saved session"):
        world.engine().attach(0, role="builder")


def test_commands_find_the_run_from_inside_its_worktree(world: World) -> None:
    world.engine().start_feature("Add mul to calc", slug=None)
    worktree = worktree_dir(world.data, "pilot", 0, "add-mul-to-calc")

    [row] = world.engine(cwd=worktree / "src").runs(all_repos=False)
    detail = world.engine(worktree).describe(0)
    stopped = world.engine(cwd=worktree).stop(0)

    assert (row.run.repo, row.run.id) == ("pilot", 0)
    assert detail.run_folder == world.folder()
    assert stopped.state == "stopped"


# ------------------------------------------------------------------ progress output


def test_start_tells_the_progress_observer_every_event_as_it_is_written(world: World) -> None:
    seen: list[Event] = []

    world.engine(progress=seen.append).start_feature("Add mul to calc", slug=None)

    assert seen == read_events(world.folder())
    assert {type(e.body).__name__ for e in seen} >= {"RunCreated", "StageStarted", "GateResult"}


def test_a_decision_and_the_rerun_reach_the_progress_observer(world: World) -> None:
    world.planner.plans.append(PLAN)
    world.engine().start_feature("Add mul to calc", slug=None)
    before = len(read_events(world.folder()))
    seen: list[Event] = []

    world.engine(progress=seen.append).send_back(0, "Keep it small.")

    assert seen == read_events(world.folder(), after=before)
    assert type(seen[0].body).__name__ == "Decision"


def test_a_broken_progress_observer_doesnt_stop_the_run(world: World) -> None:
    def broken(event: Event) -> None:
        raise RuntimeError("the terminal went away")

    done = world.engine(progress=broken).start_feature("Add mul to calc", slug=None)

    assert done.stop == "plan_approval"
    assert done.run.state == "awaiting_plan_approval"
