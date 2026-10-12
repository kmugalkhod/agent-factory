"""Tests for factory_engine.gates.post_stage (task 2.15): the engine's check after every stage,
against a temporary remote, the user's checkout, a run worktree and a data folder."""

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from factory_engine.config import RoleSettings, default_settings
from factory_engine.errors import GateError
from factory_engine.events import EventLog, GateResult, NeedsYou, read_events
from factory_engine.gates.post_stage import (
    ALLOWED_PATHS,
    MAIN_CHECKOUT,
    OUTSIDE_WORKTREE,
    SECRETS,
    Watch,
    check_stage,
    post_stage_check,
    take_baseline,
)
from factory_engine.git_ops import Git, create_worktree
from factory_engine.run import Role, Run, RunState, load_run, new_run, save_run

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
REPO = "pilot"
SLUG = "status-json"
RUN_ID = 3
GITHUB_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
STAGE_STATE: dict[Role, RunState] = {
    "planner": "planning",
    "tester": "testing",
    "builder": "building",
    "reviewer": "reviewing",
}


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


class FakeRegistry:
    def __init__(self) -> None:
        self.runs: list[Run] = []

    def upsert_run(self, run: Run) -> None:
        self.runs.append(run)


@dataclass
class Stage:
    env: dict[str, str]
    repo: Path  # the user's main checkout
    data: Path
    worktree: Path
    run_folder: Path
    config_dir: Path
    watch: Watch

    @property
    def git(self) -> Git:
        return Git(env=self.env)

    def run(self, cwd: Path, *args: str) -> str:
        return sh(cwd, *args, env=self.env)

    def write(self, root: Path, rel: str, text: str) -> None:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


@pytest.fixture
def stage(tmp_path: Path, git_env: dict[str, str]) -> Stage:
    remote = tmp_path / "remote.git"
    sh(tmp_path, "init", "--bare", "-b", "main", str(remote), env=git_env)
    seed = tmp_path / "seed"
    sh(tmp_path, "init", "-b", "main", str(seed), env=git_env)
    sh(seed, "remote", "add", "origin", str(remote), env=git_env)
    (seed / "README.md").write_text("pilot\n", encoding="utf-8")
    (seed / ".gitignore").write_text("__pycache__/\n*.log\n", encoding="utf-8")
    for rel, text in {
        "src/app.py": "def app() -> int:\n    return 1\n",
        "tests/test_app.py": "def test_app() -> None:\n    assert True\n",
    }.items():
        (seed / rel).parent.mkdir(parents=True, exist_ok=True)
        (seed / rel).write_text(text, encoding="utf-8")
    sh(seed, "add", "-A", env=git_env)
    sh(seed, "commit", "-m", "first", env=git_env)
    sh(seed, "push", "origin", "main", env=git_env)
    repo = tmp_path / "repo"
    sh(tmp_path, "clone", str(remote), str(repo), env=git_env)
    data = tmp_path / "data"
    data.mkdir()
    git = Git(env=git_env)
    worktree = create_worktree(git, repo, data, repo_name=REPO, run_id=RUN_ID, slug=SLUG)
    run_folder = data / "runs" / REPO / f"{RUN_ID}-{SLUG}"
    run_folder.mkdir(parents=True)
    config_dir = data / "agents" / REPO / f"{RUN_ID}-{SLUG}" / "tester"
    config_dir.mkdir(parents=True)
    (data / "registry.db").write_text("db", encoding="utf-8")
    (data / "worktrees" / REPO / "9-other").mkdir(parents=True)
    (data / "worktrees" / REPO / "9-other" / "keep.txt").write_text("x", encoding="utf-8")
    watch = Watch.for_stage(
        repo=repo, data=data, worktree=worktree, run_folder=run_folder, config_dir=config_dir
    )
    return Stage(git_env, repo, data, worktree, run_folder, config_dir, watch)


def role_settings(role: Role) -> RoleSettings:
    return getattr(default_settings("win32").roles, role)


def results_by_gate(results: list[GateResult]) -> dict[str, GateResult]:
    return {result.gate: result for result in results}


def failed_gates(results: list[GateResult]) -> list[str]:
    return [result.gate for result in results if not result.passed]


def check(stage: Stage, role: Role, change: Callable[[], None], **kw: object) -> list[GateResult]:
    baseline = take_baseline(stage.git, stage.watch)
    change()
    locked = kw.get("locked", ())
    assert isinstance(locked, tuple)
    return check_stage(
        stage.git, stage.watch, baseline, role=role, settings=role_settings(role), locked=locked
    )


# --- a clean stage ---


def test_a_clean_tester_stage_passes_every_check(stage: Stage) -> None:
    def change() -> None:
        stage.write(stage.worktree, "tests/test_status.py", "def test_status() -> None: ...\n")
        stage.write(stage.worktree, "tests/__pycache__/x.pyc", "ignored build output")
        stage.write(stage.run_folder, "handoff-tester.md", "# Handoff\n")
        stage.write(stage.run_folder, "run.json", "{}")
        stage.write(stage.run_folder, "events.jsonl", "")
        stage.write(stage.config_dir, "projects/session.jsonl", "transcript")
        stage.write(stage.data, "registry.db", "db changed")
        stage.write(stage.data, "registry.db-wal", "wal")

    results = check(stage, "tester", change)
    assert [r.gate for r in results] == [MAIN_CHECKOUT, OUTSIDE_WORKTREE, ALLOWED_PATHS, SECRETS]
    assert failed_gates(results) == []


def test_a_planner_stage_that_only_writes_its_plan_passes(stage: Stage) -> None:
    results = check(stage, "planner", lambda: stage.write(stage.run_folder, "plan.md", "# Plan\n"))
    assert failed_gates(results) == []


def test_a_stage_that_changed_nothing_passes(stage: Stage) -> None:
    assert failed_gates(check(stage, "reviewer", lambda: None)) == []


def test_the_diff_counts_commits_the_agent_made_itself(stage: Stage) -> None:
    def change() -> None:
        stage.write(stage.worktree, "src/leak.py", f"TOKEN = '{GITHUB_TOKEN}'\n")
        stage.run(stage.worktree, "add", "-A")
        stage.run(stage.worktree, "commit", "-m", "agent commit")

    results = results_by_gate(check(stage, "tester", change))
    assert not results[ALLOWED_PATHS].passed
    assert not results[SECRETS].passed


def _commit_then_remove(stage: Stage, rel: str, text: str, message: str = "agent commit") -> None:
    stage.write(stage.worktree, rel, text)
    stage.run(stage.worktree, "add", "-A")
    stage.run(stage.worktree, "commit", "-m", message)
    (stage.worktree / rel).unlink()
    stage.run(stage.worktree, "add", "-A")
    stage.run(stage.worktree, "commit", "-m", "tidy up")


def test_a_secret_committed_then_deleted_during_the_stage_fails(stage: Stage) -> None:
    def change() -> None:
        _commit_then_remove(stage, "tests/test_leak.py", f"TOKEN = '{GITHUB_TOKEN}'\n")

    results = results_by_gate(check(stage, "tester", change))
    assert failed_gates(list(results.values())) == [SECRETS]
    assert "tests/test_leak.py" in results[SECRETS].reason
    assert GITHUB_TOKEN not in results[SECRETS].reason


def test_a_secret_committed_then_deleted_without_a_commit_fails(stage: Stage) -> None:
    def change() -> None:
        stage.write(stage.worktree, "tests/test_leak.py", f"TOKEN = '{GITHUB_TOKEN}'\n")
        stage.run(stage.worktree, "add", "-A")
        stage.run(stage.worktree, "commit", "-m", "agent commit")
        (stage.worktree / "tests/test_leak.py").unlink()  # the stage commit removes it later

    results = results_by_gate(check(stage, "tester", change))
    assert not results[SECRETS].passed


def test_a_secret_in_a_commit_message_fails(stage: Stage) -> None:
    def change() -> None:
        stage.write(stage.worktree, "tests/test_new.py", "x = 1\n")
        stage.run(stage.worktree, "add", "-A")
        stage.run(stage.worktree, "commit", "-m", f"token {GITHUB_TOKEN}")

    results = results_by_gate(check(stage, "tester", change))
    assert failed_gates(list(results.values())) == [SECRETS]
    assert GITHUB_TOKEN not in results[SECRETS].reason


def test_a_path_touched_by_a_commit_and_put_back_fails(stage: Stage) -> None:
    def change() -> None:
        _commit_then_remove(stage, "src/sneaky.py", "X = 1\n")

    results = results_by_gate(check(stage, "tester", change))
    assert failed_gates(list(results.values())) == [ALLOWED_PATHS]
    assert "src/sneaky.py" in results[ALLOWED_PATHS].reason


# --- 1. the main checkout ---


def _edit_main_file(stage: Stage) -> None:
    stage.write(stage.repo, "README.md", "changed by a test script\n")


def _new_main_file(stage: Stage) -> None:
    stage.write(stage.repo, ".env", "SECRET=1\n")  # untracked and maybe ignored: still a change


def _stage_in_main(stage: Stage) -> None:
    stage.write(stage.repo, "README.md", "staged\n")
    stage.run(stage.repo, "add", "README.md")
    stage.write(stage.repo, "README.md", "pilot\n")  # the file is back, the index is not


def _commit_in_main(stage: Stage) -> None:
    stage.run(stage.repo, "commit", "--allow-empty", "-m", "moved HEAD")


def _switch_main_branch(stage: Stage) -> None:
    stage.run(stage.repo, "switch", "-c", "elsewhere")


def _plant_main_hook(stage: Stage) -> None:
    stage.write(stage.repo, ".git/hooks/pre-commit", "#!/bin/sh\necho owned\n")


@pytest.mark.parametrize(
    ("change", "says"),
    [
        (_edit_main_file, "README.md"),
        (_new_main_file, ".env"),
        (_stage_in_main, "index"),
        (_commit_in_main, "HEAD"),
        (_switch_main_branch, "HEAD"),
        (_plant_main_hook, "pre-commit"),
    ],
    ids=["edited-file", "new-file", "index", "commit", "branch", "hook"],
)
def test_a_changed_main_checkout_fails(
    stage: Stage, change: Callable[[Stage], None], says: str
) -> None:
    results = results_by_gate(check(stage, "tester", lambda: change(stage)))
    assert not results[MAIN_CHECKOUT].passed
    assert says in results[MAIN_CHECKOUT].reason
    assert str(stage.repo) in results[MAIN_CHECKOUT].reason


def _linked_checkout(stage: Stage, tmp_path: Path) -> Path:
    """The user's checkout is itself a linked worktree of `stage.repo`: its `.git` is a file."""
    linked = tmp_path / "linked"
    stage.run(stage.repo, "worktree", "add", "-b", "user-branch", str(linked))
    stage.watch = Watch(
        repo=linked,
        worktree=stage.watch.worktree,
        run_folder=stage.watch.run_folder,
        watched=stage.watch.watched,
        allowed=stage.watch.allowed,
    )
    return linked


@pytest.mark.parametrize(
    ("rel", "says"),
    [
        ("hooks/pre-commit", "pre-commit"),
        ("config", "config"),
        ("info/exclude", "exclude"),
    ],
    ids=["hook", "config", "info"],
)
def test_the_shared_git_folder_of_a_linked_checkout_is_watched(
    stage: Stage, tmp_path: Path, rel: str, says: str
) -> None:
    _linked_checkout(stage, tmp_path)
    shared = stage.repo / ".git"
    results = results_by_gate(check(stage, "tester", lambda: stage.write(shared, rel, "x\n")))
    assert not results[MAIN_CHECKOUT].passed
    assert says in results[MAIN_CHECKOUT].reason


def test_a_linked_checkout_left_alone_passes(stage: Stage, tmp_path: Path) -> None:
    _linked_checkout(stage, tmp_path)
    assert failed_gates(check(stage, "reviewer", lambda: None)) == []


# --- 2. outside the worktree ---


@pytest.mark.parametrize(
    "rel",
    [
        "stray.txt",
        "worktrees/pilot/9-other/keep.txt",
        "agents/pilot/3-status-json/builder/settings.json",
        "runs/pilot/2-older/plan.md",
    ],
    ids=["data-root", "other-worktree", "other-role-config", "other-run"],
)
def test_a_change_in_a_watched_folder_fails(stage: Stage, rel: str) -> None:
    results = results_by_gate(check(stage, "tester", lambda: stage.write(stage.data, rel, "x")))
    assert failed_gates(list(results.values())) == [OUTSIDE_WORKTREE]
    assert Path(rel).name in results[OUTSIDE_WORKTREE].reason


def test_a_deleted_file_in_a_watched_folder_fails(stage: Stage) -> None:
    target = stage.data / "worktrees" / REPO / "9-other" / "keep.txt"
    results = results_by_gate(check(stage, "tester", target.unlink))
    assert not results[OUTSIDE_WORKTREE].passed
    assert "keep.txt" in results[OUTSIDE_WORKTREE].reason


def test_an_extra_watched_folder_is_checked(stage: Stage, tmp_path: Path) -> None:
    other = tmp_path / "other-repo"
    other.mkdir()
    stage.watch = Watch(
        repo=stage.watch.repo,
        worktree=stage.watch.worktree,
        run_folder=stage.watch.run_folder,
        watched=(*stage.watch.watched, other),
        allowed=stage.watch.allowed,
    )
    results = results_by_gate(check(stage, "tester", lambda: stage.write(other, "x.py", "x")))
    assert not results[OUTSIDE_WORKTREE].passed
    assert "x.py" in results[OUTSIDE_WORKTREE].reason


@pytest.mark.parametrize(
    ("role", "rel"),
    [("tester", "plan.md"), ("tester", "notes.txt"), ("reviewer", "handoff-reviewer.md")],
)
def test_a_run_folder_file_that_is_not_the_roles_own_fails(
    stage: Stage, role: Role, rel: str
) -> None:
    results = results_by_gate(check(stage, role, lambda: stage.write(stage.run_folder, rel, "x")))
    assert not results[OUTSIDE_WORKTREE].passed
    assert rel in results[OUTSIDE_WORKTREE].reason


# --- 3. the role's allowed paths ---


def _remove(stage: Stage, rel: str) -> Callable[[], None]:
    return lambda: (stage.worktree / rel).unlink()


@pytest.mark.parametrize(
    ("role", "rel"),
    [
        ("tester", "src/app.py"),
        ("builder", "tests/test_app.py"),
        ("builder", "factory.yaml"),
        ("planner", "src/app.py"),
        ("reviewer", "review-notes.md"),
    ],
)
def test_a_write_outside_the_roles_paths_fails(stage: Stage, role: Role, rel: str) -> None:
    results = results_by_gate(
        check(stage, role, lambda: stage.write(stage.worktree, rel, "changed\n"))
    )
    assert failed_gates(list(results.values())) == [ALLOWED_PATHS]
    assert rel in results[ALLOWED_PATHS].reason


def test_a_delete_outside_the_roles_paths_fails(stage: Stage) -> None:
    results = results_by_gate(check(stage, "tester", _remove(stage, "src/app.py")))
    assert not results[ALLOWED_PATHS].passed
    assert "src/app.py" in results[ALLOWED_PATHS].reason


def test_the_builder_may_not_change_a_locked_tester_file(stage: Stage) -> None:
    def change() -> None:
        stage.write(stage.worktree, "src/app.py", "def app() -> int:\n    return 2\n")
        stage.write(stage.worktree, "src/locked_test.py", "changed\n")

    results = results_by_gate(check(stage, "builder", change, locked=("src/locked_test.py",)))
    assert not results[ALLOWED_PATHS].passed
    assert "src/locked_test.py" in results[ALLOWED_PATHS].reason
    assert "src/app.py" not in results[ALLOWED_PATHS].reason


def test_the_builder_may_write_its_own_paths(stage: Stage) -> None:
    results = check(stage, "builder", lambda: stage.write(stage.worktree, "src/new.py", "X = 1\n"))
    assert failed_gates(results) == []


# --- 4. the secret scan ---


@pytest.mark.parametrize(
    "change",
    [
        lambda s: s.write(s.worktree, "tests/test_token.py", f"TOKEN = '{GITHUB_TOKEN}'\n"),
        lambda s: s.write(
            s.worktree, "tests/test_app.py", f"def test_app() -> None:\n    t = '{GITHUB_TOKEN}'\n"
        ),
    ],
    ids=["new-file", "edited-file"],
)
def test_a_secret_in_the_diff_fails(stage: Stage, change: Callable[[Stage], None]) -> None:
    results = results_by_gate(check(stage, "tester", lambda: change(stage)))
    assert failed_gates(list(results.values())) == [SECRETS]
    reason = results[SECRETS].reason
    assert "GitHub token" in reason
    assert "tests/" in reason
    assert GITHUB_TOKEN not in reason


def test_a_secret_in_an_ignored_file_is_not_in_the_diff(stage: Stage) -> None:
    results = check(
        stage, "tester", lambda: stage.write(stage.worktree, "tests/run.log", GITHUB_TOKEN)
    )
    assert failed_gates(results) == []


def test_removing_a_secret_is_clean(stage: Stage) -> None:
    stage.write(stage.worktree, "tests/test_old.py", f"T = '{GITHUB_TOKEN}'\n")
    stage.run(stage.worktree, "add", "-A")
    stage.run(stage.worktree, "commit", "-m", "old secret")
    results = check(stage, "tester", _remove(stage, "tests/test_old.py"))
    assert failed_gates(results) == []


# --- fail closed ---


def test_a_check_that_cant_run_fails_closed(stage: Stage) -> None:
    def change() -> None:
        stage.run(stage.repo, "worktree", "remove", "--force", str(stage.worktree))

    results = check(stage, "tester", change)
    assert ALLOWED_PATHS in failed_gates(results)
    assert SECRETS in failed_gates(results)


def test_a_baseline_whose_git_call_fails_raises_gate_error(stage: Stage, tmp_path: Path) -> None:
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    stage.watch = Watch(
        repo=stage.watch.repo,
        worktree=not_a_repo,
        run_folder=stage.watch.run_folder,
        watched=stage.watch.watched,
        allowed=stage.watch.allowed,
    )
    with pytest.raises(GateError, match="before the stage"):
        take_baseline(stage.git, stage.watch)


# --- stopping the run ---


def _run_in(stage: Stage, role: Role) -> tuple[Run, EventLog]:
    run = new_run(
        run_id=RUN_ID,
        slug=SLUG,
        repo=REPO,
        kind="feature",
        provider="subscription",
        isolation="worktree",
        now=NOW,
    ).model_copy(update={"state": STAGE_STATE[role], "stage": role})
    save_run(run, stage.run_folder)
    return run, EventLog(stage.run_folder, repo=REPO, run_id=RUN_ID, clock=lambda: NOW)


def _post_stage(stage: Stage, role: Role, change: Callable[[], None]) -> tuple[Run, FakeRegistry]:
    run, events = _run_in(stage, role)
    baseline = take_baseline(stage.git, stage.watch)
    change()
    registry = FakeRegistry()
    outcome = post_stage_check(
        stage.git,
        stage.watch,
        baseline,
        role=role,
        settings=role_settings(role),
        run=run,
        events=events,
        registry=registry,
    )
    assert outcome.passed == all(result.passed for result in outcome.results)
    return outcome.run, registry


@pytest.mark.parametrize(
    ("gate", "change"),
    [
        (MAIN_CHECKOUT, lambda s: s.write(s.repo, "README.md", "changed\n")),
        (OUTSIDE_WORKTREE, lambda s: s.write(s.data, "stray.txt", "x")),
        (ALLOWED_PATHS, lambda s: s.write(s.worktree, "src/app.py", "changed\n")),
        (SECRETS, lambda s: s.write(s.worktree, "tests/test_t.py", f"T = '{GITHUB_TOKEN}'\n")),
    ],
    ids=["main-checkout", "outside-worktree", "allowed-paths", "secrets"],
)
def test_each_failure_stops_the_run_with_its_own_gate_result(
    stage: Stage, gate: str, change: Callable[[Stage], None]
) -> None:
    run, registry = _post_stage(stage, "tester", lambda: change(stage))
    assert run.state == "needs_you"
    assert load_run(stage.run_folder).state == "needs_you"
    assert registry.runs[-1].state == "needs_you"
    bodies = [event.body for event in read_events(stage.run_folder)]
    gates = [b for b in bodies if isinstance(b, GateResult)]
    assert [g.gate for g in gates if not g.passed] == [gate]
    assert {g.gate for g in gates} == {MAIN_CHECKOUT, OUTSIDE_WORKTREE, ALLOWED_PATHS, SECRETS}
    flagged = [b for b in bodies if isinstance(b, NeedsYou)]
    assert len(flagged) == 1
    assert gate in flagged[0].reason


def test_a_clean_stage_passes_and_the_run_carries_on(stage: Stage) -> None:
    run, registry = _post_stage(
        stage, "tester", lambda: stage.write(stage.worktree, "tests/test_new.py", "x = 1\n")
    )
    assert run.state == "testing"
    assert load_run(stage.run_folder).state == "testing"
    assert registry.runs == []
    bodies = [event.body for event in read_events(stage.run_folder)]
    assert [b.passed for b in bodies if isinstance(b, GateResult)] == [True] * 4
    assert not any(isinstance(b, NeedsYou) for b in bodies)
