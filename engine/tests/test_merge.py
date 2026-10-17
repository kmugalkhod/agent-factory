"""Tests for factory_engine.merge (task 2.22): the update before review, push, pull request and
merge, and the merge approval in factory_engine.decisions.

Each test carries a real run through the pipeline with the `GitFlow` hooks: a repo cloned from a
bare "remote", the run's worktree made by `git_ops.create_worktree`, scripted fake agents that
write real files (so the real post-stage check, gates, pytest runs and stage commits happen) and
a fake `gh` on PATH that records every call. Main "moves" when another clone pushes to it.
"""

import ast
import asyncio
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import factory_engine
from factory_engine import events as ev
from factory_engine.agents.fake import FakeAgentRunner, result, system_init
from factory_engine.agents.runner import AgentResult, AgentSpec, SessionRecorder, not_running
from factory_engine.config import Settings, default_settings, merge_settings, parse_overrides
from factory_engine.decisions import DecisionContext, approve_merge
from factory_engine.errors import DecisionError
from factory_engine.events import EventLog, read_events
from factory_engine.git_ops import Git, create_worktree, rebase_in_progress, run_branch
from factory_engine.github import GitHub
from factory_engine.merge import (
    CLEANUP_GATE,
    MERGE_REBASES,
    MERGE_UPDATE_GATE,
    GitFlow,
    approval_text,
    pr_title,
)
from factory_engine.paths import run_dir
from factory_engine.pipeline import TEST_LOCK_GATE, Hooks, Pipeline, PipelineResult, Places
from factory_engine.providers import get_profile
from factory_engine.registry import Registry, registry_path
from factory_engine.run import (
    ApprovedMerge,
    PullRequestInfo,
    Role,
    Run,
    RunState,
    load_run,
    new_run,
    save_run,
)
from factory_engine.states import move

START = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
REPO = "pilot"
RUN_ID = 1
SLUG = "add-mul"
BRANCH = run_branch(RUN_ID, SLUG)
FACTORY = Path(__file__).resolve().parents[2]
PYTHON = shlex.quote(Path(sys.executable).as_posix())
PYTEST = f"{PYTHON} -m pytest"
PARENT_ENV = {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": "test-oauth-token"}
PR_URL = "https://github.com/kunal/pilot/pull/7"

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
TESTER_TEST = "def test_mul():\n    from calc import mul\n\n    assert mul(2, 3) == 6\n"
ADD = BASE_FILES["src/calc.py"]
SUB = "\n\ndef sub(a, b):\n    return a - b\n"
MUL = "\n\ndef mul(a, b):\n    return a * b\n"
GOOD_BUILD = ADD + MUL
BETTER_BUILD = ADD + '\n\ndef mul(a, b):\n    """The product of a and b."""\n    return a * b\n'
WRONG_BUILD = ADD + "\n\ndef mul(a, b):\n    return a + b\n"
SUB_ON_MAIN = {"src/calc.py": ADD + SUB}  # conflicts with the builder's change at the file's end
RESOLVED = ADD + SUB + MUL
UNRELATED_ON_MAIN = {"src/extra.py": "EXTRA = 1\n"}
BREAKING_ON_MAIN = {"tests/test_extra.py": "def test_extra():\n    assert False\n"}

REVIEW_APPROVE = """# Review

## Acceptance criteria

- AC1: met. `tests/test_mul.py::test_mul` passes.

## Findings

None

## Verdict

Verdict: approve
"""
REVIEW_IMPORTANT = """# Review

## Acceptance criteria

- AC1: not met. `mul` has no docstring, which the repo's conventions require.

## Findings

- [Important] `src/calc.py:5`: `mul` has no docstring. Fix: Add one.

## Verdict

Verdict: request changes
"""

FAKE_GH = """\
import json, os, sys
args = sys.argv[1:]
entry = {"args": args, "cwd": os.getcwd()}
if "--body-file" in args:
    with open(args[args.index("--body-file") + 1], encoding="utf-8") as handle:
        entry["body"] = handle.read()
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(entry) + "\\n")
if args[:2] == ["pr", "merge"] and os.environ.get("FAKE_GH_FAIL_MERGE"):
    sys.stderr.write("GraphQL: Pull request is not mergeable (mergePullRequest)\\n")
    sys.exit(1)
if args[:2] == ["pr", "create"]:
    print(os.environ["FAKE_GH_URL"])
"""


def handoff(text: str) -> str:
    return f"# Handoff\n\n## Files touched\n\n{text}\n\n## Decisions\n\nNone\n"


def sh(cwd: Path, *args: str, env: dict[str, str]) -> str:
    """Run git directly, not through the code under test. Returns stdout, stripped."""
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


def write(root: Path, files: dict[str, str]) -> None:
    """Write each file's bytes exactly (`\\n` stays LF on every platform)."""
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))


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
    """One agent run: the files it writes in the worktree and the run folder."""

    role: Role
    worktree: dict[str, str] = field(default_factory=dict)
    docs: dict[str, str] = field(default_factory=dict)


class Agents:
    """An `AgentRunner` that plays one `Act` per agent run, in order."""

    def __init__(self, acts: Sequence[Act]) -> None:
        self.acts = list(acts)
        self.specs: list[AgentSpec] = []
        self._fake: FakeAgentRunner | None = None

    async def run(self, spec: AgentSpec, recorder: SessionRecorder) -> AgentResult:
        assert self.acts, f"no act left in the script for the {spec.role}"
        act = self.acts.pop(0)
        assert act.role == spec.role, f"the script expected the {act.role}, not the {spec.role}"
        self.specs.append(spec)
        write(spec.worktree, act.worktree)
        write(spec.run_folder, act.docs)
        session = spec.resume or f"{spec.role}-{len(self.specs)}"
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


def tester() -> Act:
    return Act(
        "tester",
        worktree={"tests/test_mul.py": TESTER_TEST},
        docs={"handoff-tester.md": handoff("tests/test_mul.py")},
    )


def builder(code: str | None = GOOD_BUILD) -> Act:
    """The builder writes `src/calc.py` (None: writes no code, only its handoff)."""
    files = {} if code is None else {"src/calc.py": code}
    return Act("builder", worktree=files, docs={"handoff-builder.md": handoff("src/calc.py")})


def reviewer(review: str = REVIEW_APPROVE) -> Act:
    return Act("reviewer", docs={"review.md": review})


class FakeGh:
    """A `gh` on PATH that records each call; `pr create` prints the pull request's URL."""

    def __init__(self, tmp_path: Path) -> None:
        self.bin = tmp_path / "fakebin"
        self.bin.mkdir()
        script = self.bin / "fake_gh.py"
        script.write_text(FAKE_GH, encoding="utf-8")
        if sys.platform == "win32":
            launcher = f'@"{sys.executable}" "{script}" %*\r\n'
            (self.bin / "gh.cmd").write_text(launcher, encoding="utf-8")
        else:
            launcher = f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n'
            (self.bin / "gh").write_text(launcher, encoding="utf-8")
            (self.bin / "gh").chmod(0o755)
        self.log = tmp_path / "gh-calls.jsonl"
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_GH_LOG": str(self.log),
            "FAKE_GH_URL": PR_URL,
        }

    @property
    def gh(self) -> GitHub:
        return GitHub(env=self.env)

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        lines = self.log.read_text(encoding="utf-8").splitlines()
        return [json.loads(line)["args"] for line in lines]

    def merges(self) -> list[list[str]]:
        return [args for args in self.calls() if args[:2] == ["pr", "merge"]]

    def creates(self) -> list[list[str]]:
        return [args for args in self.calls() if args[:2] == ["pr", "create"]]


# ------------------------------------------------------------------ the world


class World:
    """A bare remote with `main`, a clone (the user's checkout), a second clone that moves main,
    the run's worktree, the data folder and one run starting in `state`."""

    def __init__(self, tmp_path: Path, git_env: dict[str, str], state: RunState) -> None:
        self.env = git_env
        self.time = FakeTime(START)
        self.git = Git(git_env)
        self.remote = tmp_path / "remote.git"
        sh(tmp_path, "init", "--bare", "-b", "main", str(self.remote), env=git_env)
        self.seed = tmp_path / "seed"
        sh(tmp_path, "init", "-b", "main", str(self.seed), env=git_env)
        sh(self.seed, "config", "core.autocrlf", "false", env=git_env)
        sh(self.seed, "remote", "add", "origin", str(self.remote), env=git_env)
        write(self.seed, BASE_FILES)
        sh(self.seed, "add", "-A", env=git_env)
        sh(self.seed, "commit", "-m", "base", env=git_env)
        sh(self.seed, "push", "origin", "main", env=git_env)
        self.repo = tmp_path / "repo"
        sh(tmp_path, "clone", str(self.remote), str(self.repo), env=git_env)
        self.data = tmp_path / "data"
        self.data.mkdir()
        self.worktree = create_worktree(
            self.git, self.repo, self.data, repo_name=REPO, run_id=RUN_ID, slug=SLUG
        )
        self.run_folder = run_dir(self.data, REPO, RUN_ID, SLUG)
        self.run_folder.mkdir(parents=True)
        write(self.run_folder, {"intent.md": INTENT, "plan.md": PLAN})
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
        self.fake_gh = FakeGh(tmp_path)
        self.ctx = DecisionContext(
            run_folder=self.run_folder,
            events=self.events,
            registry=self.registry,
            clock=self.time.clock,
        )

    def flow(self) -> GitFlow:
        return GitFlow(self.fake_gh.gh)

    def advance(self, agents: Agents, hooks: Hooks | None = None) -> PipelineResult:
        """Build a pipeline from the run as saved and carry it on, as the CLI does."""
        pipeline = Pipeline(
            load_run(self.run_folder),
            places=self.places,
            settings=self.settings,
            profile=get_profile("subscription"),
            parent_env=PARENT_ENV,
            git=self.git,
            runner=agents,
            events=self.events,
            registry=self.registry,
            clock=self.time.clock,
            sleep=self.time.sleep,
            hooks=hooks or self.flow(),
        )
        return asyncio.run(pipeline.advance())

    def push_main(self, files: dict[str, str]) -> str:
        """Move main on the remote: commit `files` from the second clone. Returns the commit."""
        sh(self.seed, "fetch", "origin", env=self.env)
        sh(self.seed, "checkout", "-B", "main", "origin/main", env=self.env)
        write(self.seed, files)
        sh(self.seed, "add", "-A", env=self.env)
        sh(self.seed, "commit", "-m", f"main: {', '.join(files)}", env=self.env)
        sh(self.seed, "push", "origin", "HEAD:refs/heads/main", env=self.env)
        return sh(self.seed, "rev-parse", "HEAD", env=self.env)

    def head(self) -> str:
        return sh(self.worktree, "rev-parse", "HEAD", env=self.env)

    def remote_sha(self, ref: str) -> str:
        return sh(self.remote, "rev-parse", ref, env=self.env)

    def rebasing(self) -> bool:
        return rebase_in_progress(self.git, self.worktree, run_id=RUN_ID, slug=SLUG)

    def run(self) -> Run:
        return load_run(self.run_folder)

    def save(self, run: Run) -> None:
        save_run(run, self.run_folder)
        self.registry.upsert_run(run)

    def needs_you_reasons(self) -> list[str]:
        return [
            event.body.reason
            for event in read_events(self.run_folder)
            if isinstance(event.body, ev.NeedsYou)
        ]

    def close(self) -> None:
        self.registry.close()


type Maker = Callable[[RunState], World]


@pytest.fixture
def world_at(tmp_path: Path, git_env: dict[str, str]) -> Iterator[Maker]:
    made: list[World] = []

    def make(state: RunState = "testing") -> World:
        made.append(World(tmp_path, git_env, state))
        return made[-1]

    yield make
    for each in made:
        each.close()


def summary(folder: Path) -> list[str]:
    """The run's events that tell its story: states, stages, gates (not each post-stage check),
    decisions and stops."""
    out: list[str] = []
    for event in read_events(folder):
        body = event.body
        if isinstance(body, ev.StateChanged):
            out.append(f"state {body.from_state}>{body.to_state}")
        elif isinstance(body, ev.StageStarted):
            out.append(f"start {body.stage} {body.attempt}")
        elif isinstance(body, ev.GateResult) and not body.gate.startswith("post_stage."):
            out.append(f"gate {body.gate} {'pass' if body.passed else 'fail'}")
        elif isinstance(body, ev.Decision):
            out.append(f"decision {body.action}")
        elif isinstance(body, ev.NeedsYou):
            out.append("needs_you")
    return out


class MovingMain(GitFlow):
    """The git flow, with main moving on the remote right before the first update."""

    def __init__(self, world: World, files: dict[str, str]) -> None:
        super().__init__(world.fake_gh.gh)
        self.world = world
        self.files = files
        self.before: str | None = None  # the run branch's head before the first update

    async def update(self, pipeline: Pipeline, run: Run) -> Run:
        if self.before is None:
            self.before = self.world.head()
            self.world.push_main(self.files)
        return await super().update(pipeline, run)


class PushedRunBranch(GitFlow):
    """The git flow, with someone else pushing the run branch to the remote before the first
    update, so the engine's safe force-push must refuse."""

    def __init__(self, world: World) -> None:
        super().__init__(world.fake_gh.gh)
        self.world = world
        self.done = False

    async def update(self, pipeline: Pipeline, run: Run) -> Run:
        if not self.done:
            self.done = True
            sh(self.world.seed, "push", "origin", f"HEAD:refs/heads/{BRANCH}", env=self.world.env)
        return await super().update(pipeline, run)


# ------------------------------------------------------------------ the paths


def to_awaiting_merge(world: World) -> PipelineResult:
    done = world.advance(Agents([tester(), builder(), reviewer()]))
    assert (done.stop, done.run.state) == ("awaiting_merge", "awaiting_merge")
    return done


def path_waits_for_approval(world: World) -> None:
    to_awaiting_merge(world)
    for _ in range(2):  # advance again while it waits: nothing runs, nothing merges
        assert world.advance(Agents([])).run.state == "awaiting_merge"


def path_review_sends_back(world: World) -> None:
    acts = [tester(), builder(), reviewer(REVIEW_IMPORTANT), builder(BETTER_BUILD), reviewer()]
    assert world.advance(Agents(acts)).run.state == "awaiting_merge"


def path_builder_cap(world: World) -> None:
    acts = [tester(), builder(WRONG_BUILD), builder(WRONG_BUILD), builder(WRONG_BUILD)]
    assert world.advance(Agents(acts)).run.state == "needs_you"


def path_conflict_resolved(world: World) -> None:
    acts = [tester(), builder(), builder(RESOLVED), reviewer()]
    hooks = MovingMain(world, SUB_ON_MAIN)
    assert world.advance(Agents(acts), hooks).run.state == "awaiting_merge"


def path_conflict_unresolved(world: World) -> None:
    acts = [tester(), builder(), builder(None), builder(None)]
    hooks = MovingMain(world, SUB_ON_MAIN)
    assert world.advance(Agents(acts), hooks).run.state == "needs_you"


def path_tests_fail_after_update(world: World) -> None:
    hooks = MovingMain(world, BREAKING_ON_MAIN)
    assert world.advance(Agents([tester(), builder()]), hooks).run.state == "needs_you"


def path_push_refused(world: World) -> None:
    hooks = PushedRunBranch(world)
    acts = [tester(), builder(), reviewer()]
    assert world.advance(Agents(acts), hooks).run.state == "needs_you"


def path_forged_approval(world: World) -> None:
    """An approval written into run.json without your approve action is refused."""
    run = to_awaiting_merge(world).run
    assert run.pull_request is not None
    head = run.pull_request.head
    world.save(run.model_copy(update={"merge_approval": ApprovedMerge(pr_number=7, head=head)}))
    assert world.advance(Agents([])).run.state == "needs_you"


def path_stale_approval(world: World) -> None:
    """An approval of an older head doesn't merge a newer one."""
    to_awaiting_merge(world)
    approve_merge(world.ctx)
    run = world.run()
    assert run.pull_request is not None
    moved = run.pull_request.model_copy(update={"head": "f" * 40})
    world.save(run.model_copy(update={"pull_request": moved}))
    assert world.advance(Agents([])).run.state == "needs_you"


def path_merge_time_conflict(world: World) -> None:
    to_awaiting_merge(world)
    approve_merge(world.ctx)
    world.push_main(SUB_ON_MAIN)
    assert world.advance(Agents([])).run.state == "needs_you"


def path_approval_refused_before_the_pull_request(world: World) -> None:
    with pytest.raises(DecisionError):
        approve_merge(world.ctx)
    to_awaiting_merge(world)


PATHS: dict[str, Callable[[World], None]] = {
    "waits-for-approval": path_waits_for_approval,
    "review-sends-back": path_review_sends_back,
    "builder-cap": path_builder_cap,
    "conflict-resolved": path_conflict_resolved,
    "conflict-unresolved": path_conflict_unresolved,
    "tests-fail-after-update": path_tests_fail_after_update,
    "push-refused": path_push_refused,
    "forged-approval": path_forged_approval,
    "stale-approval": path_stale_approval,
    "merge-time-conflict": path_merge_time_conflict,
    "approval-refused-early": path_approval_refused_before_the_pull_request,
}


@pytest.mark.parametrize("path", list(PATHS), ids=list(PATHS))
def test_no_path_merges_without_an_approve_action(world_at: Maker, path: str) -> None:
    world = world_at("testing")
    PATHS[path](world)
    assert world.fake_gh.merges() == []
    assert world.run().state != "merged"


def test_only_merge_approved_calls_gh_pr_merge() -> None:
    """Structurally: `github.merge_pr` is called from `merge.merge_approved` only, and only
    `merge_pr` builds a `gh pr merge` command."""
    package = Path(factory_engine.__file__).parent
    callers: set[tuple[str, str]] = set()
    commands: set[str] = set()
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(func):
                if isinstance(node, ast.Call):
                    called = node.func
                    name = called.id if isinstance(called, ast.Name) else None
                    if isinstance(called, ast.Attribute):
                        name = called.attr
                    if name == "merge_pr":
                        callers.add((path.name, func.name))
                if isinstance(node, ast.List) and [
                    elt.value for elt in node.elts[:2] if isinstance(elt, ast.Constant)
                ] == ["pr", "merge"]:
                    commands.add(f"{path.name}:{func.name}")
    assert callers == {("merge.py", "merge_approved")}
    assert commands == {"github.py:merge_pr"}


# ------------------------------------------------------------------ update before review


def test_the_happy_path_pushes_and_opens_the_pull_request_then_waits(world_at: Maker) -> None:
    world = world_at("testing")
    done = to_awaiting_merge(world)

    assert summary(world.run_folder) == [
        "start tester 1",
        "gate tests pass",
        "state testing>building",
        "start builder 1",
        "gate build pass",
        "state building>updating",
        "gate update pass",
        "state updating>reviewing",
        "start reviewer 1",
        "gate review pass",
        "state reviewing>awaiting_merge",
    ]
    head = world.head()
    assert world.remote_sha(f"refs/heads/{BRANCH}") == head
    pr = done.run.pull_request
    assert pr is not None and (pr.number, pr.url, pr.head, pr.rebased_from) == (
        7,
        PR_URL,
        head,
        None,
    )
    assert world.run().pull_request == pr
    assert world.registry.get_run(REPO, RUN_ID) == world.run()
    [create] = world.fake_gh.creates()
    assert create[create.index("--head") + 1] == BRANCH
    assert create[create.index("--base") + 1] == "main"
    assert create[create.index("--title") + 1] == pr_title(done.run) == "factory(run-1): add mul"
    body = (world.run_folder / "pr-body.md").read_text(encoding="utf-8")
    assert "Add `mul(a, b)`" in body and "## Test output" in body and "Verdict: approve" in body
    assert world.fake_gh.merges() == []


def test_a_rebase_conflict_goes_to_the_builder_and_the_tests_rerun(world_at: Maker) -> None:
    world = world_at("testing")
    agents = Agents([tester(), builder(), builder(RESOLVED), reviewer()])
    hooks = MovingMain(world, SUB_ON_MAIN)

    done = world.advance(agents, hooks)

    assert done.run.state == "awaiting_merge"
    assert summary(world.run_folder) == [
        "start tester 1",
        "gate tests pass",
        "state testing>building",
        "start builder 1",
        "gate build pass",
        "state building>updating",
        "gate update fail",
        "state updating>building",
        "start builder 2",
        "gate build pass",
        "state building>updating",
        "gate update pass",
        "state updating>reviewing",
        "start reviewer 1",
        "gate review pass",
        "state reviewing>awaiting_merge",
    ]
    first, second = agents.of("builder")
    assert (first.resume, second.resume) == (None, "builder-2")  # the builder resumes
    assert "src/calc.py" in second.prompt and "conflict" in second.prompt
    assert not world.rebasing()
    main = world.remote_sha("refs/heads/main")
    sh(world.worktree, "merge-base", "--is-ancestor", main, "HEAD", env=world.env)  # on main
    assert (world.worktree / "src/calc.py").read_text(encoding="utf-8") == RESOLVED
    assert done.run.attempts["builder"] == 2
    assert world.remote_sha(f"refs/heads/{BRANCH}") == world.head()


def test_an_unresolved_conflict_needs_you_and_gives_the_rebase_up(world_at: Maker) -> None:
    world = world_at("testing")
    agents = Agents([tester(), builder(), builder(None), builder(None)])
    hooks = MovingMain(world, SUB_ON_MAIN)

    done = world.advance(agents, hooks)

    assert done.run.state == "needs_you"
    assert done.run.attempts["builder"] == 3
    assert summary(world.run_folder)[-4:] == [
        "gate update fail",  # conflicts left after the third attempt
        "state building>needs_you",
        "needs_you",
        "gate update fail",  # the rebase given up
    ]
    assert "cap of 3" in world.needs_you_reasons()[-1]
    assert not world.rebasing()
    assert world.head() == hooks.before  # the run branch is back where it was
    assert world.fake_gh.calls() == []


def test_a_conflict_with_the_builder_at_its_cap_needs_you_at_once(world_at: Maker) -> None:
    world = world_at("testing")
    world.settings = merge_settings(
        world.settings, parse_overrides({"caps": {"builder_attempts": 1}}, "test overrides")
    )
    hooks = MovingMain(world, SUB_ON_MAIN)

    done = world.advance(Agents([tester(), builder()]), hooks)

    assert done.run.state == "needs_you"
    [reason] = world.needs_you_reasons()
    assert "src/calc.py" in reason and "all its attempts" in reason
    assert not world.rebasing()
    assert world.head() == hooks.before


def test_tests_failing_after_a_clean_rebase_need_you(world_at: Maker) -> None:
    world = world_at("testing")
    hooks = MovingMain(world, BREAKING_ON_MAIN)

    done = world.advance(Agents([tester(), builder()]), hooks)

    assert done.run.state == "needs_you"
    assert summary(world.run_folder)[-3:] == [
        "gate update fail",
        "state updating>needs_you",
        "needs_you",
    ]
    assert "tests fail after the rebase" in world.needs_you_reasons()[-1]
    assert world.fake_gh.calls() == []


def test_a_refused_push_needs_you_and_opens_no_pull_request(world_at: Maker) -> None:
    world = world_at("testing")
    done = world.advance(Agents([tester(), builder(), reviewer()]), PushedRunBranch(world))

    assert done.run.state == "needs_you"
    assert "couldn't be pushed" in world.needs_you_reasons()[-1]
    assert world.fake_gh.calls() == []
    assert done.run.pull_request is None


# ------------------------------------------------------------------ merge approval


def test_approve_merge_records_the_approval_and_logs_the_decision(world_at: Maker) -> None:
    world = world_at("testing")
    run = to_awaiting_merge(world).run
    assert run.pull_request is not None

    decided = approve_merge(world.ctx)

    assert decided.note is None
    assert decided.run.state == "awaiting_merge"
    assert decided.run.merge_approval == ApprovedMerge(pr_number=7, head=run.pull_request.head)
    assert world.run() == decided.run
    assert world.registry.get_run(REPO, RUN_ID) == decided.run
    last = read_events(world.run_folder)[-1].body
    assert last == ev.Decision(action="approve_merge", text=approval_text(7, run.pull_request.head))
    assert world.fake_gh.merges() == []  # the decision itself merges nothing


@pytest.mark.parametrize("state", ["testing", "reviewing", "needs_you", "merged"])
def test_approve_merge_is_refused_unless_the_run_waits_for_its_merge(
    world_at: Maker, state: RunState
) -> None:
    world = world_at(state)
    before = world.run()
    with pytest.raises(DecisionError, match="not waiting for your merge decision"):
        approve_merge(world.ctx)
    assert world.run() == before
    assert read_events(world.run_folder) == []


def test_approve_merge_is_refused_without_a_pull_request(world_at: Maker) -> None:
    world = world_at("awaiting_merge")
    with pytest.raises(DecisionError, match="no pull request"):
        approve_merge(world.ctx)
    assert world.run().merge_approval is None


# ------------------------------------------------------------------ merge


def test_approve_then_advance_merges_the_tested_head_and_removes_the_worktree(
    world_at: Maker,
) -> None:
    world = world_at("testing")
    to_awaiting_merge(world)
    head = world.head()
    approve_merge(world.ctx)

    done = world.advance(Agents([]))

    assert (done.stop, done.run.state) == ("ended", "merged")
    assert world.fake_gh.merges() == [
        ["pr", "merge", PR_URL, "--merge", "--match-head-commit", head]
    ]
    assert summary(world.run_folder)[-3:] == [
        "decision approve_merge",
        "state awaiting_merge>merged",
        f"gate {CLEANUP_GATE} pass",
    ]
    assert not world.worktree.exists()
    listed = sh(world.repo, "worktree", "list", "--porcelain", env=world.env)
    assert str(world.worktree.name) not in listed
    assert sh(world.repo, "rev-parse", "--verify", BRANCH, env=world.env) == head  # branch kept
    run = world.run()
    assert run.merge_approval is None
    assert run.pull_request is not None and run.pull_request.head == head
    assert world.registry.get_run(REPO, RUN_ID) == run


def test_main_moving_after_approval_rebases_reruns_the_tests_and_pushes_first(
    world_at: Maker,
) -> None:
    world = world_at("testing")
    to_awaiting_merge(world)
    approved = world.head()
    approve_merge(world.ctx)
    main = world.push_main(UNRELATED_ON_MAIN)

    world.advance(Agents([]))

    run = world.run()
    assert run.state == "merged"
    assert run.pull_request is not None
    new_head = run.pull_request.head
    assert new_head != approved
    assert run.pull_request.rebased_from == approved
    assert world.remote_sha(f"refs/heads/{BRANCH}") == new_head  # pushed before the merge
    sh(world.remote, "merge-base", "--is-ancestor", main, new_head, env=world.env)  # on main
    assert world.fake_gh.merges() == [
        ["pr", "merge", PR_URL, "--merge", "--match-head-commit", new_head]
    ]
    story = summary(world.run_folder)
    assert story[story.index("decision approve_merge") :] == [
        "decision approve_merge",
        f"gate {MERGE_UPDATE_GATE} pass",
        "state awaiting_merge>merged",
        f"gate {CLEANUP_GATE} pass",
    ]
    test_runs = [e for e in read_events(world.run_folder) if isinstance(e.body, ev.TestRun)]
    assert test_runs[-1].seq > next(
        e.seq for e in read_events(world.run_folder) if isinstance(e.body, ev.Decision)
    )  # the tests reran after your approval
    assert not world.worktree.exists()


def test_a_conflict_at_merge_time_needs_you_and_leaves_the_approved_head(
    world_at: Maker,
) -> None:
    world = world_at("testing")
    to_awaiting_merge(world)
    approved = world.head()
    approve_merge(world.ctx)
    world.push_main(SUB_ON_MAIN)

    done = world.advance(Agents([]))

    assert done.run.state == "needs_you"
    assert "conflicts in src/calc.py" in world.needs_you_reasons()[-1]
    assert world.fake_gh.merges() == []
    assert not world.rebasing()
    assert world.head() == approved
    assert world.remote_sha(f"refs/heads/{BRANCH}") == approved
    assert done.run.merge_approval is None  # used once
    assert world.worktree.exists()


def test_a_failed_gh_merge_needs_you_and_keeps_the_worktree(world_at: Maker) -> None:
    world = world_at("testing")
    to_awaiting_merge(world)
    approve_merge(world.ctx)
    world.fake_gh.env["FAKE_GH_FAIL_MERGE"] = "1"

    done = world.advance(Agents([]))

    assert done.run.state == "needs_you"
    assert "not mergeable" in world.needs_you_reasons()[-1]
    assert len(world.fake_gh.merges()) == 1
    assert done.run.merge_approval is None
    assert world.worktree.exists()


def test_a_forged_approval_is_refused_before_gh_runs(world_at: Maker) -> None:
    world = world_at("testing")
    path_forged_approval(world)
    assert "no approve_merge decision" in world.needs_you_reasons()[-1]
    assert world.fake_gh.merges() == []
    assert world.run().merge_approval is None


def test_an_approval_of_an_older_head_is_refused(world_at: Maker) -> None:
    world = world_at("testing")
    path_stale_approval(world)
    assert "approve the merge again" in world.needs_you_reasons()[-1]
    assert world.fake_gh.merges() == []


def test_the_default_hooks_merge_nothing(world_at: Maker) -> None:
    world = world_at("testing")
    to_awaiting_merge(world)
    approve_merge(world.ctx)

    done = world.advance(Agents([]), Hooks())

    assert done.run.state == "needs_you"
    assert done.run.merge_approval is None
    assert world.fake_gh.merges() == []


# ------------------------------------------------------------------ review fixes: what is tested
# is exactly what is pushed, main is checked again before the merge, the test lock follows a
# clean rebase


SCRIBBLER = {
    "tests/conftest.py": (
        "from pathlib import Path\n\nPath('src/scribble.txt').write_text('left by the tests\\n')\n"
    )
}
"""A change on main whose test run leaves an untracked file in the worktree."""
CALC_TEST = BASE_FILES["tests/test_calc.py"]
TESTER_EDIT = CALC_TEST + "\n\n" + TESTER_TEST  # the tester adds its test to an existing file
MAIN_EDIT = {"tests/test_calc.py": '"""Tests for calc."""\n\n' + CALC_TEST}  # merges cleanly


def tester_editing_calc_tests() -> Act:
    return Act(
        "tester",
        worktree={"tests/test_calc.py": TESTER_EDIT},
        docs={"handoff-tester.md": handoff("tests/test_calc.py")},
    )


def lock(world: World) -> dict[str, str]:
    return json.loads((world.run_folder / "tester-files.json").read_text(encoding="utf-8"))["files"]


class MainMovesDuringTests(GitFlow):
    """The git flow, with main moving again while each of the first `times` merge-time test
    runs goes on (pushed right after the run, before the engine looks at main again)."""

    def __init__(self, world: World, times: int) -> None:
        super().__init__(world.fake_gh.gh)
        self.world = world
        self.times = times
        self.moves = 0

    def _rebase_for_merge(self, pipeline: Pipeline, run: Run, pr: PullRequestInfo) -> str | Run:
        outcome = super()._rebase_for_merge(pipeline, run, pr)
        if self.moves < self.times:
            self.moves += 1
            self.world.push_main({f"src/extra_{self.moves}.py": f"EXTRA = {self.moves}\n"})
        return outcome


def test_a_test_run_that_changes_the_worktree_after_the_update_needs_you(
    world_at: Maker,
) -> None:
    world = world_at("testing")
    hooks = MovingMain(world, SCRIBBLER)

    done = world.advance(Agents([tester(), builder()]), hooks)

    assert done.run.state == "needs_you"
    reason = world.needs_you_reasons()[-1]
    assert "scribble.txt" in reason and "isn't exactly the commit" in reason
    assert world.fake_gh.calls() == []


def test_a_test_run_that_changes_the_worktree_at_merge_time_merges_and_pushes_nothing(
    world_at: Maker,
) -> None:
    world = world_at("testing")
    to_awaiting_merge(world)
    approved = world.head()
    approve_merge(world.ctx)
    world.push_main(SCRIBBLER)

    done = world.advance(Agents([]))

    assert done.run.state == "needs_you"
    assert "scribble.txt" in world.needs_you_reasons()[-1]
    assert world.fake_gh.merges() == []
    assert world.remote_sha(f"refs/heads/{BRANCH}") == approved  # nothing pushed


def test_uncommitted_changes_at_merge_time_merge_nothing(world_at: Maker) -> None:
    world = world_at("testing")
    to_awaiting_merge(world)
    approve_merge(world.ctx)
    (world.worktree / "src" / "calc.py").write_text(WRONG_BUILD, encoding="utf-8")

    done = world.advance(Agents([]))

    assert done.run.state == "needs_you"
    assert "src/calc.py" in world.needs_you_reasons()[-1]
    assert world.fake_gh.merges() == []


def test_main_moving_during_the_merge_time_tests_is_rebased_and_tested_again(
    world_at: Maker,
) -> None:
    world = world_at("testing")
    to_awaiting_merge(world)
    approve_merge(world.ctx)
    world.push_main(UNRELATED_ON_MAIN)
    hooks = MainMovesDuringTests(world, times=1)

    world.advance(Agents([]), hooks)

    run = world.run()
    assert run.state == "merged"
    assert run.pull_request is not None
    head = run.pull_request.head
    main = world.remote_sha("refs/heads/main")
    sh(world.remote, "merge-base", "--is-ancestor", main, head, env=world.env)  # the latest main
    assert world.fake_gh.merges() == [
        ["pr", "merge", PR_URL, "--merge", "--match-head-commit", head]
    ]
    assert summary(world.run_folder).count(f"gate {MERGE_UPDATE_GATE} pass") == 2
    assert world.remote_sha(f"refs/heads/{BRANCH}") == head


def test_main_that_keeps_moving_during_the_merge_needs_you(world_at: Maker) -> None:
    world = world_at("testing")
    to_awaiting_merge(world)
    approved = world.head()
    approve_merge(world.ctx)
    world.push_main(UNRELATED_ON_MAIN)
    hooks = MainMovesDuringTests(world, times=MERGE_REBASES)

    done = world.advance(Agents([]), hooks)

    assert done.run.state == "needs_you"
    assert f"each of {MERGE_REBASES} rebases" in world.needs_you_reasons()[-1]
    assert summary(world.run_folder).count(f"gate {MERGE_UPDATE_GATE} pass") == MERGE_REBASES
    assert world.fake_gh.merges() == []
    assert world.remote_sha(f"refs/heads/{BRANCH}") == approved


def test_a_clean_rebase_that_changes_a_locked_test_re_bases_the_lock(world_at: Maker) -> None:
    world = world_at("testing")
    hooks = MovingMain(world, MAIN_EDIT)
    agents = Agents([tester_editing_calc_tests(), builder(), reviewer()])

    done = world.advance(agents, hooks)

    assert done.run.state == "awaiting_merge"
    story = summary(world.run_folder)
    assert story[story.index("state building>updating") :][:3] == [
        "state building>updating",
        f"gate {TEST_LOCK_GATE} pass",
        "gate update pass",
    ]
    rebased = sh(world.worktree, "rev-parse", "HEAD:tests/test_calc.py", env=world.env)
    assert lock(world)["tests/test_calc.py"] == rebased
    text = (world.worktree / "tests" / "test_calc.py").read_text(encoding="utf-8")
    assert text.startswith('"""Tests for calc."""') and "def test_mul" in text


def test_a_conflict_in_a_locked_test_needs_you_without_the_builder(world_at: Maker) -> None:
    world = world_at("testing")
    other = {"tests/test_mul.py": "def test_mul():\n    assert 2 * 3 == 6\n"}  # add/add conflict
    hooks = MovingMain(world, other)

    done = world.advance(Agents([tester(), builder()]), hooks)

    assert done.run.state == "needs_you"
    assert done.run.attempts["builder"] == 1  # the builder isn't asked to resolve a locked test
    reason = world.needs_you_reasons()[-1]
    assert "tests/test_mul.py" in reason and "locked" in reason
    assert not world.rebasing()
    assert world.head() == hooks.before


def test_a_test_lock_broken_before_the_merge_time_rebase_needs_you(world_at: Maker) -> None:
    world = world_at("testing")
    to_awaiting_merge(world)
    approve_merge(world.ctx)
    world.push_main(UNRELATED_ON_MAIN)
    path = world.run_folder / "tester-files.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["files"]["tests/test_mul.py"] = "0" * 40
    path.write_text(json.dumps(record), encoding="utf-8")

    done = world.advance(Agents([]))

    assert done.run.state == "needs_you"
    assert "test lock fails before the rebase" in world.needs_you_reasons()[-1]
    assert world.fake_gh.merges() == []


# ------------------------------------------------------------------ metrics and report (2.27)


def test_a_merged_run_records_its_metrics_and_writes_its_report(world_at: Maker) -> None:
    world = world_at("testing")
    opened = to_awaiting_merge(world).run
    assert opened.pull_request is not None
    assert opened.pull_request.opened_head == opened.pull_request.head == world.head()
    approve_merge(world.ctx)

    done = world.advance(Agents([]))

    run = world.run()
    assert done.run == run and run.state == "merged"
    assert run.metrics.first_pass is True
    assert run.metrics.builder_retries == 0
    assert run.metrics.important_findings == 0
    assert run.metrics.pr_lines_changed_by_you == 0
    assert [(use.role, use.attempt) for use in run.metrics.token_use] == [
        ("tester", 1),
        ("builder", 1),
        ("reviewer", 1),
    ]
    assert world.registry.get_run(REPO, RUN_ID) == run
    report = (world.run_folder / "report.md").read_text(encoding="utf-8").splitlines()
    assert "Status: merged" in report
    assert f"Pull request: {PR_URL}" in report
    assert "- AC1: met, `test_mul`" in report
    assert len(report) <= 60


def test_changes_after_the_pull_request_opened_count_as_yours_but_main_doesnt(
    world_at: Maker,
) -> None:
    """Your feedback on the open pull request (2.23) reruns the builder, and the engine moves
    the pull request to the new head. What changed in the run's own diff since the pull request
    opened is counted at the merge; what main brought in by the rebases isn't."""
    world = world_at("testing")
    opened = to_awaiting_merge(world).run
    feedback = move(opened, "building", log=world.events)
    world.save(feedback)
    world.push_main(UNRELATED_ON_MAIN)  # rebased in by the update before the second review
    done = world.advance(Agents([builder(BETTER_BUILD), reviewer()]))
    assert done.run.state == "awaiting_merge"
    assert done.run.pull_request is not None and opened.pull_request is not None
    assert done.run.pull_request.opened_head == opened.pull_request.head
    assert done.run.pull_request.head != opened.pull_request.head
    approve_merge(world.ctx)
    world.push_main({"src/more.py": "MORE = 1\nMORE_2 = 2\n"})  # rebased in at merge time

    world.advance(Agents([]))

    run = world.run()
    assert run.state == "merged"
    assert run.metrics.pr_lines_changed_by_you == 1  # the docstring line
    assert run.metrics.builder_retries == 1
    assert run.metrics.first_pass is True  # decided by the first build and review
    assert world.registry.get_run(REPO, RUN_ID) == run
    report = (world.run_folder / "report.md").read_text(encoding="utf-8")
    assert "lines you changed on the pull request: 1" in report
