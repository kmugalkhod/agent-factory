"""Git operations for a run (design: "Git flow"). Only the engine runs these; agents never run git.

- Branch: `factory/run-<id>-<slug>` from the latest main, in a new worktree under
  `<data>/worktrees/<repo>/<id>-<slug>/`.
- Commit: once after each passing stage, `factory(run-<id>): <role> attempt <n>`, empty when the
  stage changed nothing.
- Update: fetch main and rebase the run branch onto it; conflicts are reported for the builder.
- Push: the run branch only, with a lease on the commit the engine itself last pushed, so it
  refuses when the remote branch changed unexpectedly.
- Clean-up: remove a run's worktree after merge, or 7 days after a run ended without one.

Every command runs with a list of arguments and no shell. Repo hooks never run: a hook is code
from the repo (or, through `core.hooksPath`, from the worktree an agent writes), and the engine
runs with the user's credentials.
"""

import contextlib
import os
import re
import signal
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from factory_engine.errors import DataFolderError, GitError, PushRejectedError
from factory_engine.paths import check_slug, run_dir
from factory_engine.run import Role, Run, RunState

RUN_BRANCH_PREFIX = "factory/run-"
WORKTREES = "worktrees"
PUSHED_REFS = "refs/factory/pushed/"
"""Where the engine records the commit it last pushed per run branch: the push lease."""
GIT_TIMEOUT = 300.0
ABANDON_AFTER = timedelta(days=7)
FINISHED_STATES: frozenset[RunState] = frozenset({"merged", "failed", "stopped", "closed"})
"""Runs whose worktree is no longer needed. A run waiting on you keeps its worktree."""

# An absolute path that holds no hooks and that nobody can create files under.
NO_HOOKS = "//./NUL" if sys.platform == "win32" else "/dev/null"
# Variables that point git at another repository, index or object store.
_LOCATION_VARS = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
    }
)
# What cmd.exe interprets inside a batch file's arguments however they are quoted.
_BATCH_UNSAFE = re.compile(r'["%^&|<>!\r\n]')
_CONFLICT_MARKER = re.compile(r"^(<{7}|>{7})(?: |$)", re.MULTILINE)


# ------------------------------------------------------------------ running programs


def env_get(env: Mapping[str, str], key: str) -> str:
    """`env[key]`, ignoring case on Windows, where `Path` and `PATH` are the same variable."""
    if sys.platform != "win32":
        return env.get(key, "")
    return next((v for k, v in env.items() if k.upper() == key.upper()), "")


def find_program(name: str, env: Mapping[str, str]) -> Path | None:
    """`name` on the `PATH` in `env`. Relative `PATH` entries are skipped, so a program planted
    in the working folder (the worktree) is never picked up."""
    windows = sys.platform == "win32"
    if windows:
        pathext = env_get(env, "PATHEXT") or ".COM;.EXE;.BAT;.CMD"
        suffixes = [ext for ext in pathext.lower().split(";") if ext]
    else:
        suffixes = [""]
    for entry in env_get(env, "PATH").split(os.pathsep):
        folder = Path(entry.strip().strip('"'))
        if not entry.strip() or not folder.is_absolute():
            continue
        for suffix in suffixes:
            candidate = folder / f"{name}{suffix}"
            if candidate.is_file() and (windows or os.access(candidate, os.X_OK)):
                return candidate
    return None


def run_command(
    argv: Sequence[str], cwd: Path, env: Mapping[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run `argv` without a shell and return its output. On a timeout or Ctrl-C the whole
    process tree stops (git and gh start helpers such as ssh and credential managers).

    Raises `subprocess.TimeoutExpired`, `OSError`, or `ValueError` for an argument a batch file
    (`gh.cmd`) can't receive safely, since cmd.exe re-parses those.
    """
    if Path(argv[0]).suffix.lower() in {".bat", ".cmd"}:
        unsafe = [arg for arg in argv[1:] if _BATCH_UNSAFE.search(arg)]
        if unsafe:
            raise ValueError(
                f"{argv[0]} is a batch file and cmd.exe would re-read the argument "
                f"{unsafe[0]!r}. Put the real program (an .exe) first on PATH."
            )
    proc = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        start_new_session=sys.platform != "win32",
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except BaseException:
        _stop_tree(proc)
        raise
    return subprocess.CompletedProcess(list(argv), proc.returncode, out, err)


def _stop_tree(proc: subprocess.Popen[str]) -> None:
    """Kill a command and every process it started, then wait for it (as `tools/dev.py`)."""
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                check=False,
                capture_output=True,
                timeout=60,
                encoding="utf-8",
                errors="replace",
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    proc.kill()  # no-op if the tree kill worked
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        proc.communicate(timeout=60)


@dataclass(frozen=True)
class Git:
    """Runs git for the engine. `env` is the engine's environment (pass `os.environ`); variables
    that would point git at another repository are dropped, and git never prompts."""

    env: Mapping[str, str]
    timeout: float = GIT_TIMEOUT

    def run(
        self, args: Sequence[str], cwd: Path, *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        shown = "git " + " ".join(args)
        program = find_program("git", self.env)
        if program is None:
            raise GitError(f"`{shown}`: git isn't on PATH. Install Git and add it to PATH.")
        argv = [str(program), "-c", f"core.hooksPath={NO_HOOKS}", *args]
        try:
            done = run_command(argv, cwd, self._env(), self.timeout)
        except subprocess.TimeoutExpired as err:
            raise GitError(
                f"`{shown}` in {cwd} took longer than {self.timeout:g}s and was stopped. "
                "Check the network and the remote, then retry."
            ) from err
        except (OSError, ValueError) as err:
            raise GitError(f"`{shown}` in {cwd} couldn't start ({err}).") from err
        if check and done.returncode != 0:
            detail = (done.stderr.strip() or done.stdout.strip())[-2000:]
            raise GitError(f"`{shown}` in {cwd} failed (exit {done.returncode}): {detail}")
        return done

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in self.env.items() if k.upper() not in _LOCATION_VARS}
        env.update(GIT_TERMINAL_PROMPT="0", GIT_EDITOR="true", LC_ALL="C")
        return env

    def out(self, args: Sequence[str], cwd: Path) -> str:
        return self.run(args, cwd).stdout.strip()


# ------------------------------------------------------------------ names and places


def run_branch(run_id: int, slug: str) -> str:
    """`factory/run-<id>-<slug>`: the only branch the engine commits on and pushes for a run."""
    if run_id < 0:
        raise DataFolderError(f"run id {run_id} is negative. Run ids start at 0.")
    check_slug(slug)
    return f"{RUN_BRANCH_PREFIX}{run_id}-{slug}"


def worktree_dir(data: Path, repo: str, run_id: int, slug: str) -> Path:
    """`<data>/worktrees/<repo>/<id>-<slug>/`: the run's worktree, outside the repo."""
    run_dir(data, repo, run_id, slug)  # checks the repo name, id and slug
    return data / WORKTREES / repo / f"{run_id}-{slug}"


# ------------------------------------------------------------------ branch and commits


def create_worktree(
    git: Git,
    repo: Path,
    data: Path,
    *,
    repo_name: str,
    run_id: int,
    slug: str,
    remote: str = "origin",
    base: str = "main",
) -> Path:
    """Fetch `base` and create the run branch from it in a new worktree. The main checkout's
    branch, index and files are left alone. Returns the worktree folder."""
    branch = run_branch(run_id, slug)
    folder = worktree_dir(data, repo_name, run_id, slug)
    if folder.exists():
        raise GitError(
            f"{folder} already exists, so run {run_id} already has a worktree. Remove it with "
            "the run's clean-up, or start a new run."
        )
    _fetch(git, repo, remote, base)
    try:
        folder.parent.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        raise GitError(f"{folder.parent}: can't create the worktrees folder ({err}).") from err
    git.run(
        ["worktree", "add", "--no-track", "-b", branch, str(folder), _tracking(remote, base)],
        repo,
    )
    return folder


def commit_stage(
    git: Git, worktree: Path, *, run_id: int, slug: str, role: Role, attempt: int
) -> str:
    """Commit everything the stage changed in the worktree as one commit on the run branch:
    `factory(run-<id>): <role> attempt <n>`. A stage that changed nothing gets an empty commit,
    so every passing stage has exactly one. Returns the commit ID."""
    if attempt < 1:
        raise GitError(f"attempt {attempt} is invalid; attempts count from 1.")
    _require_branch(git, worktree, run_branch(run_id, slug))
    git.run(["add", "--all", "--", "."], worktree)
    message = f"factory(run-{run_id}): {role} attempt {attempt}"
    git.run(["commit", "--allow-empty", "--no-verify", "-m", message], worktree)
    return git.out(["rev-parse", "HEAD"], worktree)


# ------------------------------------------------------------------ rebase


@dataclass(frozen=True)
class RebaseResult:
    """The outcome of a rebase step. With conflicts, the rebase waits in the worktree for the
    builder to resolve the listed files (worktree-relative, `/`-separated)."""

    conflicts: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.conflicts


def rebase_onto_main(
    git: Git, worktree: Path, *, run_id: int, slug: str, remote: str = "origin", base: str = "main"
) -> RebaseResult:
    """Fetch `base` and rebase the run branch onto it, keeping empty stage commits."""
    _require_branch(git, worktree, run_branch(run_id, slug))
    _fetch(git, worktree, remote, base)
    done = git.run(
        ["rebase", "--keep-empty", "--empty=keep", _tracking(remote, base)], worktree, check=False
    )
    return _rebase_outcome(git, worktree, done)


def rebase_continue(git: Git, worktree: Path, *, run_id: int, slug: str) -> RebaseResult:
    """Carry a conflicted rebase on once the builder has edited the files. A file that still
    holds conflict markers is reported again and nothing is staged."""
    _require_rebasing(git, worktree, run_branch(run_id, slug))
    unresolved = tuple(name for name in _unmerged(git, worktree) if _has_markers(worktree / name))
    if unresolved:
        return RebaseResult(unresolved)
    git.run(["add", "--all", "--", "."], worktree)
    done = git.run(["rebase", "--continue"], worktree, check=False)
    return _rebase_outcome(git, worktree, done)


def rebase_abort(git: Git, worktree: Path, *, run_id: int, slug: str) -> None:
    """Give up a conflicted rebase; the run branch goes back to where it was."""
    _require_rebasing(git, worktree, run_branch(run_id, slug))
    git.run(["rebase", "--abort"], worktree)


def _rebase_outcome(
    git: Git, worktree: Path, done: subprocess.CompletedProcess[str]
) -> RebaseResult:
    if done.returncode == 0:
        return RebaseResult()
    conflicts = _unmerged(git, worktree)
    if conflicts:
        return RebaseResult(conflicts)
    detail = (done.stderr.strip() or done.stdout.strip())[-2000:]
    raise GitError(
        f"rebase in {worktree} failed without conflicts (exit {done.returncode}): {detail}"
    )


def _unmerged(git: Git, worktree: Path) -> tuple[str, ...]:
    out = git.run(["diff", "--name-only", "--diff-filter=U", "-z"], worktree).stdout
    return tuple(sorted({name for name in out.split("\0") if name}))


def _has_markers(path: Path) -> bool:
    try:
        return (
            _CONFLICT_MARKER.search(path.read_text(encoding="utf-8", errors="replace")) is not None
        )
    except FileNotFoundError:
        return False  # resolved by deleting the file
    except OSError:
        return True  # can't tell: treat as unresolved


def _require_rebasing(git: Git, worktree: Path, branch: str) -> None:
    head_name = Path(git.out(["rev-parse", "--git-path", "rebase-merge/head-name"], worktree))
    if not head_name.is_absolute():
        head_name = worktree / head_name
    try:
        rebasing = head_name.read_text(encoding="utf-8").strip()
    except OSError:
        rebasing = ""
    if rebasing != f"refs/heads/{branch}":
        raise GitError(
            f"{worktree} has no rebase of {branch} in progress. Start one with the update step."
        )


# ------------------------------------------------------------------ push


def push_run_branch(
    git: Git, worktree: Path, *, run_id: int, slug: str, branch: str, remote: str = "origin"
) -> None:
    """Safe force-push of the run branch, and only the run branch. The lease is the commit the
    engine last pushed (or "absent" before the first push), kept under `refs/factory/pushed/`,
    so a fetch by anyone else can't weaken it. Raises `PushRejectedError` if the remote branch
    changed unexpectedly."""
    expected = run_branch(run_id, slug)
    if branch != expected:
        raise GitError(
            f"refusing to push {branch!r}: the engine pushes only this run's run branch, "
            f"{expected}."
        )
    _require_branch(git, worktree, expected)
    ref = f"refs/heads/{expected}"
    lease = PUSHED_REFS + expected
    found = git.run(["rev-parse", "-q", "--verify", f"{lease}^{{commit}}"], worktree, check=False)
    last = found.stdout.strip() if found.returncode == 0 else ""
    head = git.out(["rev-parse", "HEAD"], worktree)
    done = git.run(
        ["push", "--porcelain", f"--force-with-lease={ref}:{last}", remote, f"{ref}:{ref}"],
        worktree,
        check=False,
    )
    if done.returncode != 0:
        detail = (done.stdout.strip() + "\n" + done.stderr.strip()).strip()[-2000:]
        if "stale info" in detail or "[rejected]" in detail or "fetch first" in detail:
            raise PushRejectedError(
                f"push of {expected} refused: the remote branch changed since the engine last "
                f"pushed it. Check who pushed to it, then decide in the run. ({detail})"
            )
        raise GitError(f"push of {expected} failed (exit {done.returncode}): {detail}")
    git.run(["update-ref", lease, head], worktree)


# ------------------------------------------------------------------ clean-up


def remove_worktree(git: Git, repo: Path, worktree: Path) -> None:
    """Remove one of the repo's linked worktrees, uncommitted files included. The run branch
    and its commits stay. Refuses the main checkout and any folder that isn't a worktree."""
    listed = [
        Path(line.removeprefix("worktree "))
        for line in git.out(["worktree", "list", "--porcelain"], repo).splitlines()
        if line.startswith("worktree ")
    ]
    linked = {_same(path) for path in listed[1:]}  # the first entry is the main checkout
    if _same(worktree) not in linked:
        raise GitError(
            f"{worktree} isn't a linked worktree of {repo}, so it wasn't removed. Only run "
            "worktrees are removed."
        )
    git.run(["worktree", "remove", "--force", str(worktree)], repo)
    git.run(["worktree", "prune"], repo)


def clean_abandoned(
    git: Git,
    repo: Path,
    data: Path,
    runs: Iterable[Run],
    *,
    now: datetime,
    max_age: timedelta = ABANDON_AFTER,
) -> list[Path]:
    """Remove the worktrees of finished runs (merged, failed, stopped, closed) last updated at
    least `max_age` ago. Returns the folders removed."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware; pass the engine clock's time.")
    removed: list[Path] = []
    for run in runs:
        if run.state not in FINISHED_STATES or now - run.updated_at < max_age:
            continue
        folder = worktree_dir(data, run.repo, run.id, run.slug)
        if folder.exists():
            remove_worktree(git, repo, folder)
            removed.append(folder)
    return removed


# ------------------------------------------------------------------ helpers


def _fetch(git: Git, cwd: Path, remote: str, base: str) -> None:
    git.run(["fetch", "--no-tags", remote, f"+refs/heads/{base}:{_tracking(remote, base)}"], cwd)


def _tracking(remote: str, base: str) -> str:
    return f"refs/remotes/{remote}/{base}"


def _require_branch(git: Git, worktree: Path, branch: str) -> None:
    head = git.run(["symbolic-ref", "-q", "HEAD"], worktree, check=False)
    current = head.stdout.strip() if head.returncode == 0 else ""
    if current != f"refs/heads/{branch}":
        on = current.removeprefix("refs/heads/") or "a detached HEAD"
        raise GitError(
            f"{worktree} is on {on}, not its run branch {branch}. The engine commits, rebases "
            "and pushes on the run branch only; check the worktree out on it again."
        )


def _same(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))
