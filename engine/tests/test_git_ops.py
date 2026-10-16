"""Tests for factory_engine.git_ops (task 2.14): run worktrees, stage commits, rebase, push and
clean-up, against temporary repos and a bare "remote"."""

import ast
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from factory_engine import git_ops
from factory_engine.errors import DataFolderError, GitError, PushRejectedError
from factory_engine.git_ops import (
    Git,
    clean_abandoned,
    commit_stage,
    create_worktree,
    push_run_branch,
    rebase_continue,
    rebase_onto_main,
    remove_worktree,
    run_branch,
    worktree_dir,
)
from factory_engine.run import Role, Run, RunState, new_run

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
REPO = "pilot"
SLUG = "status-json"


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


@dataclass(frozen=True)
class Repos:
    env: dict[str, str]
    remote: Path  # bare repo standing in for GitHub
    seed: Path  # another clone, used to move the remote's branches
    repo: Path  # the user's checkout the engine works from
    data: Path  # the factory data folder

    @property
    def git(self) -> Git:
        return Git(env=self.env)

    def run(self, cwd: Path, *args: str) -> str:
        return sh(cwd, *args, env=self.env)

    def commit_on_seed(
        self, name: str, text: str, branch: str = "main", start: str | None = None
    ) -> str:
        """Commit `name` on `branch` (made from `start` if given) in the seed clone and push it.
        Returns the new commit."""
        self.run(self.seed, "fetch", "origin")
        self.run(self.seed, "checkout", "-B", branch, f"origin/{start or branch}")
        (self.seed / name).write_text(text, encoding="utf-8")
        self.run(self.seed, "add", "-A")
        self.run(self.seed, "commit", "-m", f"seed: {name}")
        self.run(self.seed, "push", "origin", f"HEAD:refs/heads/{branch}")
        return self.run(self.seed, "rev-parse", "HEAD")

    def remote_sha(self, ref: str) -> str:
        return self.run(self.remote, "rev-parse", ref)

    def worktree(self, run_id: int = 3, slug: str = SLUG) -> Path:
        return create_worktree(
            self.git, self.repo, self.data, repo_name=REPO, run_id=run_id, slug=slug
        )


@pytest.fixture
def repos(tmp_path: Path, git_env: dict[str, str]) -> Repos:
    remote = tmp_path / "remote.git"
    sh(tmp_path, "init", "--bare", "-b", "main", str(remote), env=git_env)
    seed = tmp_path / "seed"
    sh(tmp_path, "init", "-b", "main", str(seed), env=git_env)
    sh(seed, "remote", "add", "origin", str(remote), env=git_env)
    (seed / "README.md").write_text("pilot\n", encoding="utf-8")
    sh(seed, "add", "-A", env=git_env)
    sh(seed, "commit", "-m", "first", env=git_env)
    sh(seed, "push", "origin", "main", env=git_env)
    repo = tmp_path / "repo"
    sh(tmp_path, "clone", str(remote), str(repo), env=git_env)
    data = tmp_path / "data"
    data.mkdir()
    return Repos(env=git_env, remote=remote, seed=seed, repo=repo, data=data)


def _commits_since(repos: Repos, cwd: Path, base: str) -> list[str]:
    out = repos.run(cwd, "rev-list", f"{base}..HEAD")
    return out.split() if out else []


# --- names and places ---


def test_run_branch_name() -> None:
    assert run_branch(3, SLUG) == "factory/run-3-status-json"


@pytest.mark.parametrize("slug", ["", "Bad Slug", "../x", "a" * 41])
def test_run_branch_refuses_a_bad_slug(slug: str) -> None:
    with pytest.raises(DataFolderError):
        run_branch(3, slug)


def test_worktree_lives_under_the_data_folder(tmp_path: Path) -> None:
    assert worktree_dir(tmp_path, REPO, 3, SLUG) == tmp_path / "worktrees" / REPO / "3-status-json"


def test_worktree_dir_refuses_an_unsafe_repo_name(tmp_path: Path) -> None:
    with pytest.raises(DataFolderError):
        worktree_dir(tmp_path, "../other", 3, SLUG)


# --- branch ---


def test_worktree_starts_from_the_latest_main_not_the_stale_local_one(repos: Repos) -> None:
    latest = repos.commit_on_seed("later.txt", "moved on\n")  # the local clone hasn't fetched it
    worktree = repos.worktree()
    assert worktree == worktree_dir(repos.data, REPO, 3, SLUG)
    assert repos.run(worktree, "rev-parse", "HEAD") == latest
    assert repos.run(worktree, "branch", "--show-current") == "factory/run-3-status-json"
    assert (worktree / "later.txt").is_file()


def test_creating_the_worktree_leaves_the_main_checkout_alone(repos: Repos) -> None:
    before = repos.run(repos.repo, "rev-parse", "HEAD")
    repos.commit_on_seed("later.txt", "moved on\n")
    repos.worktree()
    assert repos.run(repos.repo, "rev-parse", "HEAD") == before
    assert repos.run(repos.repo, "branch", "--show-current") == "main"
    assert repos.run(repos.repo, "status", "--porcelain") == ""


def test_creating_a_worktree_twice_for_the_same_run_raises(repos: Repos) -> None:
    repos.worktree()
    with pytest.raises(GitError):
        repos.worktree()


def test_an_existing_run_branch_is_not_reused(repos: Repos) -> None:
    repos.run(repos.repo, "branch", "factory/run-3-status-json")
    with pytest.raises(GitError):
        repos.worktree()


# --- commits ---


def test_stage_commit_has_the_run_message_and_lands_on_the_run_branch(repos: Repos) -> None:
    worktree = repos.worktree()
    main_before = repos.run(repos.repo, "rev-parse", "main")
    (worktree / "status.py").write_text("STATUS = 'ok'\n", encoding="utf-8")
    sha = commit_stage(repos.git, worktree, run_id=3, slug=SLUG, role="builder", attempt=2)
    assert repos.run(worktree, "log", "-1", "--format=%s") == "factory(run-3): builder attempt 2"
    assert repos.run(worktree, "rev-parse", "factory/run-3-status-json") == sha
    assert repos.run(worktree, "show", "--name-only", "--format=", sha) == "status.py"
    assert repos.run(repos.repo, "rev-parse", "main") == main_before
    assert repos.run(worktree, "status", "--porcelain") == ""


def test_stage_commit_takes_deletions_and_new_folders(repos: Repos) -> None:
    worktree = repos.worktree()
    (worktree / "README.md").unlink()
    (worktree / "pkg").mkdir()
    (worktree / "pkg" / "mod.py").write_text("X = 1\n", encoding="utf-8")
    sha = commit_stage(repos.git, worktree, run_id=3, slug=SLUG, role="builder", attempt=1)
    changed = repos.run(worktree, "show", "--name-status", "--format=", sha).splitlines()
    assert sorted(changed) == ["A\tpkg/mod.py", "D\tREADME.md"]


UNCHANGING: list[Role] = ["planner", "reviewer"]


@pytest.mark.parametrize("role", UNCHANGING)
def test_a_stage_that_changed_nothing_gets_exactly_one_empty_commit(
    repos: Repos, role: Role
) -> None:
    worktree = repos.worktree()
    start = repos.run(worktree, "rev-parse", "HEAD")
    sha = commit_stage(repos.git, worktree, run_id=3, slug=SLUG, role=role, attempt=1)
    assert _commits_since(repos, worktree, start) == [sha]
    assert repos.run(worktree, "show", "--name-only", "--format=", sha) == ""
    assert repos.run(worktree, "log", "-1", "--format=%s") == f"factory(run-3): {role} attempt 1"


def test_stage_commit_refuses_when_the_worktree_is_on_another_branch(repos: Repos) -> None:
    worktree = repos.worktree()
    repos.run(worktree, "switch", "-c", "elsewhere")
    (worktree / "x.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(GitError, match="factory/run-3-status-json"):
        commit_stage(repos.git, worktree, run_id=3, slug=SLUG, role="builder", attempt=1)
    assert repos.run(worktree, "rev-parse", "elsewhere") == repos.run(worktree, "rev-parse", "main")


def test_stage_commit_refuses_a_detached_head(repos: Repos) -> None:
    worktree = repos.worktree()
    repos.run(worktree, "switch", "--detach")
    with pytest.raises(GitError):
        commit_stage(repos.git, worktree, run_id=3, slug=SLUG, role="builder", attempt=1)


def test_repo_hooks_do_not_run_on_engine_commands(repos: Repos) -> None:
    # A hook is code from the repo (or, through core.hooksPath, from the worktree an agent
    # writes), so the engine never runs one.
    marker = repos.repo / "hook-ran"
    hooks = repos.repo / ".git" / "hooks"
    for name in ("pre-commit", "commit-msg", "post-commit", "post-checkout", "pre-push"):
        script = f"#!/bin/sh\necho {name} >> '{marker.as_posix()}'\nexit 1\n"
        (hooks / name).write_text(script, encoding="utf-8", newline="\n")
        (hooks / name).chmod(0o755)
    worktree = repos.worktree()
    commit_stage(repos.git, worktree, run_id=3, slug=SLUG, role="planner", attempt=1)
    push_run_branch(repos.git, worktree, run_id=3, slug=SLUG, branch="factory/run-3-status-json")
    assert not marker.exists()


# --- rebase ---


def test_rebase_moves_the_run_branch_onto_the_latest_main(repos: Repos) -> None:
    worktree = repos.worktree()
    (worktree / "status.py").write_text("STATUS = 'ok'\n", encoding="utf-8")
    commit_stage(repos.git, worktree, run_id=3, slug=SLUG, role="builder", attempt=1)
    latest = repos.commit_on_seed("other.txt", "from main\n")
    result = rebase_onto_main(repos.git, worktree, run_id=3, slug=SLUG)
    assert result.conflicts == ()
    assert result.ok
    repos.run(worktree, "merge-base", "--is-ancestor", latest, "HEAD")  # raises if not
    assert repos.run(worktree, "branch", "--show-current") == "factory/run-3-status-json"
    assert (worktree / "status.py").is_file()
    assert (worktree / "other.txt").is_file()


def test_rebase_with_conflicts_reports_the_files_and_waits(repos: Repos) -> None:
    worktree = repos.worktree()
    (worktree / "README.md").write_text("run edit\n", encoding="utf-8")
    commit_stage(repos.git, worktree, run_id=3, slug=SLUG, role="builder", attempt=1)
    repos.commit_on_seed("README.md", "main edit\n")
    result = rebase_onto_main(repos.git, worktree, run_id=3, slug=SLUG)
    assert not result.ok
    assert result.conflicts == ("README.md",)
    # The builder resolves the file; the engine then carries the rebase on.
    (worktree / "README.md").write_text("both edits\n", encoding="utf-8")
    after = rebase_continue(repos.git, worktree, run_id=3, slug=SLUG)
    assert after.ok
    assert repos.run(worktree, "branch", "--show-current") == "factory/run-3-status-json"
    assert repos.run(worktree, "show", "HEAD:README.md") == "both edits"


def test_rebase_continue_with_markers_left_reports_the_conflict_again(repos: Repos) -> None:
    worktree = repos.worktree()
    (worktree / "README.md").write_text("run edit\n", encoding="utf-8")
    commit_stage(repos.git, worktree, run_id=3, slug=SLUG, role="builder", attempt=1)
    repos.commit_on_seed("README.md", "main edit\n")
    rebase_onto_main(repos.git, worktree, run_id=3, slug=SLUG)
    again = rebase_continue(repos.git, worktree, run_id=3, slug=SLUG)
    assert again.conflicts == ("README.md",)


@pytest.mark.parametrize(
    "left",
    [
        "run edit\n=======\nmain edit\n",
        "run edit\r\n=======\r\nmain edit\r\n",
        "run edit\n||||||| base\nseed\n",
        "run edit\n|||||||\nseed\n",
        "run edit\n>>>>>>> main\n",
    ],
    ids=["separator", "separator-crlf", "diff3-base", "diff3-base-bare", "end-marker"],
)
def test_rebase_continue_with_part_of_a_conflict_left_reports_it_again(
    repos: Repos, left: str
) -> None:
    worktree = repos.worktree()
    (worktree / "README.md").write_text("run edit\n", encoding="utf-8")
    commit_stage(repos.git, worktree, run_id=3, slug=SLUG, role="builder", attempt=1)
    repos.commit_on_seed("README.md", "main edit\n")
    rebase_onto_main(repos.git, worktree, run_id=3, slug=SLUG)
    (worktree / "README.md").write_bytes(left.encode("utf-8"))
    again = rebase_continue(repos.git, worktree, run_id=3, slug=SLUG)
    assert again.conflicts == ("README.md",)


def test_rebase_in_progress_is_true_only_while_a_conflict_waits(repos: Repos) -> None:
    worktree = repos.worktree()
    assert not git_ops.rebase_in_progress(repos.git, worktree, run_id=3, slug=SLUG)
    (worktree / "README.md").write_text("run edit\n", encoding="utf-8")
    commit_stage(repos.git, worktree, run_id=3, slug=SLUG, role="builder", attempt=1)
    repos.commit_on_seed("README.md", "main edit\n")
    rebase_onto_main(repos.git, worktree, run_id=3, slug=SLUG)
    assert git_ops.rebase_in_progress(repos.git, worktree, run_id=3, slug=SLUG)
    git_ops.rebase_abort(repos.git, worktree, run_id=3, slug=SLUG)
    assert not git_ops.rebase_in_progress(repos.git, worktree, run_id=3, slug=SLUG)
    assert repos.run(worktree, "branch", "--show-current") == "factory/run-3-status-json"


def test_main_moved_tells_whether_main_has_commits_the_branch_lacks(repos: Repos) -> None:
    worktree = repos.worktree()
    commit_stage(repos.git, worktree, run_id=3, slug=SLUG, role="builder", attempt=1)
    assert not git_ops.main_moved(repos.git, worktree)
    repos.commit_on_seed("other.txt", "from main\n")
    assert git_ops.main_moved(repos.git, worktree)  # it fetches main first
    rebase_onto_main(repos.git, worktree, run_id=3, slug=SLUG)
    assert not git_ops.main_moved(repos.git, worktree)


def test_rebase_refuses_when_the_worktree_is_on_another_branch(repos: Repos) -> None:
    worktree = repos.worktree()
    repos.run(worktree, "switch", "-c", "elsewhere")
    with pytest.raises(GitError):
        rebase_onto_main(repos.git, worktree, run_id=3, slug=SLUG)


# --- push ---


def test_push_publishes_the_run_branch(repos: Repos) -> None:
    worktree = repos.worktree()
    sha = commit_stage(repos.git, worktree, run_id=3, slug=SLUG, role="planner", attempt=1)
    push_run_branch(repos.git, worktree, run_id=3, slug=SLUG, branch="factory/run-3-status-json")
    assert repos.remote_sha("refs/heads/factory/run-3-status-json") == sha


def test_push_after_a_rebase_force_pushes_the_rewritten_branch(repos: Repos) -> None:
    worktree = repos.worktree()
    (worktree / "status.py").write_text("STATUS = 'ok'\n", encoding="utf-8")
    commit_stage(repos.git, worktree, run_id=3, slug=SLUG, role="builder", attempt=1)
    branch = "factory/run-3-status-json"
    push_run_branch(repos.git, worktree, run_id=3, slug=SLUG, branch=branch)
    repos.commit_on_seed("other.txt", "from main\n")
    assert rebase_onto_main(repos.git, worktree, run_id=3, slug=SLUG).ok
    push_run_branch(repos.git, worktree, run_id=3, slug=SLUG, branch=branch)
    assert repos.remote_sha(f"refs/heads/{branch}") == repos.run(worktree, "rev-parse", "HEAD")


def test_push_refuses_when_the_remote_branch_changed_unexpectedly(repos: Repos) -> None:
    worktree = repos.worktree()
    branch = "factory/run-3-status-json"
    commit_stage(repos.git, worktree, run_id=3, slug=SLUG, role="planner", attempt=1)
    push_run_branch(repos.git, worktree, run_id=3, slug=SLUG, branch=branch)
    theirs = repos.commit_on_seed("someone.txt", "pushed by someone else\n", branch=branch)
    commit_stage(repos.git, worktree, run_id=3, slug=SLUG, role="tester", attempt=1)
    with pytest.raises(PushRejectedError):
        push_run_branch(repos.git, worktree, run_id=3, slug=SLUG, branch=branch)
    assert repos.remote_sha(f"refs/heads/{branch}") == theirs


def test_push_refuses_when_a_branch_appeared_on_the_remote_before_the_first_push(
    repos: Repos,
) -> None:
    worktree = repos.worktree()
    branch = "factory/run-3-status-json"
    theirs = repos.commit_on_seed("someone.txt", "already there\n", branch=branch, start="main")
    commit_stage(repos.git, worktree, run_id=3, slug=SLUG, role="planner", attempt=1)
    with pytest.raises(PushRejectedError):
        push_run_branch(repos.git, worktree, run_id=3, slug=SLUG, branch=branch)
    assert repos.remote_sha(f"refs/heads/{branch}") == theirs


def test_a_fetch_of_the_run_branch_does_not_weaken_the_lease(repos: Repos) -> None:
    # A background `git fetch` (an IDE, a user) moves the remote-tracking ref; the lease must
    # still hold against what the engine itself last pushed.
    worktree = repos.worktree()
    branch = "factory/run-3-status-json"
    commit_stage(repos.git, worktree, run_id=3, slug=SLUG, role="planner", attempt=1)
    push_run_branch(repos.git, worktree, run_id=3, slug=SLUG, branch=branch)
    theirs = repos.commit_on_seed("someone.txt", "pushed by someone else\n", branch=branch)
    repos.run(repos.repo, "fetch", "origin")
    commit_stage(repos.git, worktree, run_id=3, slug=SLUG, role="tester", attempt=1)
    with pytest.raises(PushRejectedError):
        push_run_branch(repos.git, worktree, run_id=3, slug=SLUG, branch=branch)
    assert repos.remote_sha(f"refs/heads/{branch}") == theirs


@pytest.mark.parametrize(
    "branch",
    [
        "main",
        "refs/heads/main",
        "factory/run-4-status-json",
        "factory/run-3-other",
        "factory/run-3-status-json:main",
        "HEAD",
        "+factory/run-3-status-json",
        "",
    ],
)
def test_pushing_any_branch_but_the_run_branch_raises(repos: Repos, branch: str) -> None:
    worktree = repos.worktree()
    commit_stage(repos.git, worktree, run_id=3, slug=SLUG, role="planner", attempt=1)
    main_before = repos.remote_sha("refs/heads/main")
    with pytest.raises(GitError, match="run branch"):
        push_run_branch(repos.git, worktree, run_id=3, slug=SLUG, branch=branch)
    assert repos.remote_sha("refs/heads/main") == main_before
    assert repos.run(repos.remote, "branch", "--list", "factory/*") == ""


# --- clean-up ---


def test_remove_worktree_deletes_the_folder_and_keeps_the_branch(repos: Repos) -> None:
    worktree = repos.worktree()
    sha = commit_stage(repos.git, worktree, run_id=3, slug=SLUG, role="planner", attempt=1)
    (worktree / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")
    remove_worktree(repos.git, repos.repo, worktree)
    assert not worktree.exists()
    assert str(worktree.name) not in repos.run(repos.repo, "worktree", "list")
    assert repos.run(repos.repo, "rev-parse", "factory/run-3-status-json") == sha


def test_remove_worktree_refuses_a_folder_outside_the_worktrees(repos: Repos) -> None:
    with pytest.raises(GitError):
        remove_worktree(repos.git, repos.repo, repos.repo)
    assert (repos.repo / "README.md").is_file()


def _run_at(run_id: int, slug: str, state: RunState, age: timedelta) -> Run:
    run = new_run(
        run_id=run_id,
        slug=slug,
        repo=REPO,
        kind="feature",
        provider="subscription",
        isolation="worktree",
        now=NOW - age,
    )
    return run.model_copy(update={"state": state})


def test_clean_up_removes_only_worktrees_of_runs_abandoned_7_days_ago(repos: Repos) -> None:
    old_failed = _run_at(1, "old-failed", "failed", timedelta(days=7, minutes=1))
    old_stopped = _run_at(2, "old-stopped", "stopped", timedelta(days=30))
    recent_failed = _run_at(3, "recent-failed", "failed", timedelta(days=6, hours=23))
    old_waiting = _run_at(4, "old-waiting", "awaiting_merge", timedelta(days=30))
    old_needs_you = _run_at(5, "old-needs-you", "needs_you", timedelta(days=30))
    runs = [old_failed, old_stopped, recent_failed, old_waiting, old_needs_you]
    folders = {run.id: repos.worktree(run.id, run.slug) for run in runs}
    removed = clean_abandoned(repos.git, repos.repo, repos.data, runs, now=NOW)
    assert sorted(removed) == sorted([folders[1], folders[2]])
    assert not folders[1].exists()
    assert not folders[2].exists()
    assert all(folders[i].is_dir() for i in (3, 4, 5))


def test_clean_up_skips_runs_whose_worktree_is_already_gone(repos: Repos) -> None:
    gone = _run_at(1, "gone", "closed", timedelta(days=8))
    assert clean_abandoned(repos.git, repos.repo, repos.data, [gone], now=NOW) == []


def test_clean_up_refuses_a_naive_clock(repos: Repos) -> None:
    with pytest.raises(ValueError):
        clean_abandoned(repos.git, repos.repo, repos.data, [], now=datetime(2026, 10, 11))


# --- how commands run ---


def test_leftover_git_dir_in_the_environment_is_ignored(repos: Repos, tmp_path: Path) -> None:
    other = tmp_path / "other"
    sh(tmp_path, "init", "-b", "main", str(other), env=repos.env)
    env = {**repos.env, "GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other)}
    worktree = create_worktree(
        Git(env=env), repos.repo, repos.data, repo_name=REPO, run_id=3, slug=SLUG
    )
    assert repos.run(worktree, "branch", "--show-current") == "factory/run-3-status-json"
    assert repos.run(other, "branch", "--list") == ""


def test_a_git_failure_raises_with_the_command_and_its_error(repos: Repos, tmp_path: Path) -> None:
    nowhere = tmp_path / "nowhere"
    nowhere.mkdir()
    with pytest.raises(GitError, match="git fetch"):
        create_worktree(repos.git, nowhere, repos.data, repo_name=REPO, run_id=3, slug=SLUG)


def test_git_missing_from_path_raises(tmp_path: Path) -> None:
    git = Git(env={"PATH": str(tmp_path)})
    with pytest.raises(GitError, match="git"):
        git.run(["status"], tmp_path)


def test_programs_in_the_working_folder_are_not_picked_up(tmp_path: Path) -> None:
    # An agent can write files into the worktree; a `git.exe` or `gh.cmd` there must not run.
    for name in ("git", "git.exe", "git.cmd", "git.bat"):
        (tmp_path / name).write_text("planted\n", encoding="utf-8")
    with pytest.raises(GitError):
        Git(env={"PATH": "."}).run(["status"], tmp_path)


def test_no_engine_git_or_github_call_uses_a_shell() -> None:
    root = Path(git_ops.__file__).parent
    for name in ("git_ops.py", "github.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                assert all(kw.arg != "shell" for kw in node.keywords), f"{name}: shell= used"
