"""The engine's check after every stage (design: safety, control 5).

Hooks see the commands an agent runs, not what they do inside, so a test script could still
write outside the worktree. Before a stage the engine takes a `Baseline`; after it,
`check_stage` compares and checks four things, each its own `gate_result`:

1. `post_stage.main_checkout`: the main checkout is unchanged. HEAD (branch and commit), the
   index entries, every file in the working tree (ignored ones too) and the git hooks and config
   that git runs or reads (`config`, `config.worktree`, `hooks/`, `info/` in the checkout's git
   folder and, when the checkout is itself a linked worktree, in the shared one).
2. `post_stage.outside_worktree`: nothing changed in the watched folders (the data folder by
   default: other runs, other worktrees, other agents' config folders), except the places that
   may change during a stage. In the run folder only the role's own run docs and the engine's
   `run.json` and `events.jsonl` may change.
3. `post_stage.allowed_paths`: every path the stage's diff touches (added, changed or deleted,
   committed or not) is one the role may write, judged by the same rules as the path hook.
4. `post_stage.secrets`: the secret scan of the diff's added lines is clean.

The diff is the worktree against the commit it started the stage on, untracked files included
and ignored ones left out, which is what the stage commit will hold. The push sends every commit
made during the stage, so checks 3 and 4 also read each of those commits (and their messages):
a path touched or a secret added and then taken back out still fails. Each commit is read on its
own and file names exactly as git wrote them, so no byte in a name or a message can hide one
path or secret from the checks. Files outside git are
compared by type, size, mode and modification time. A check that can't run fails (fail closed);
any failure moves the run to needs_you (`post_stage_check`).
"""

import hashlib
import os
import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from factory_engine.agents.runner import RunIndex, persist
from factory_engine.config import RoleSettings
from factory_engine.errors import GateError, GitError
from factory_engine.events import EVENTS_FILE, EventLog, GateResult, NeedsYou
from factory_engine.git_ops import Git
from factory_engine.registry import REGISTRY_FILE
from factory_engine.run import RUN_FILE, Role, Run
from factory_engine.safety.paths import PathPolicy, check_file_tool
from factory_engine.safety.secrets import SecretFinding, scan_diff, scan_text
from factory_engine.states import move

MAIN_CHECKOUT = "post_stage.main_checkout"
OUTSIDE_WORKTREE = "post_stage.outside_worktree"
ALLOWED_PATHS = "post_stage.allowed_paths"
SECRETS = "post_stage.secrets"
GATES = (MAIN_CHECKOUT, OUTSIDE_WORKTREE, ALLOWED_PATHS, SECRETS)

ENGINE_FILES = frozenset({RUN_FILE, EVENTS_FILE})  # the engine writes these during a stage
# in the main checkout's git folders (its own and, for a linked checkout, the shared one)
GIT_WATCHED = ("config", "config.worktree", "hooks", "info")
SQLITE_SUFFIXES = ("", "-wal", "-shm", "-journal")
MAX_LISTED = 10
BINARY_PROBE = 8000  # bytes git looks at to decide a file is binary
# The diff the secret scan reads: added lines only, and no helpers from the repo's config.
DIFF_OPTIONS = (
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--no-renames",
    "--src-prefix=a/",
    "--dst-prefix=b/",
    "-U0",
)

# (kind, size, mode, modification time in ns); a folder is ("dir", 0, 0, 0), since its time
# changes whenever a skipped place inside it does.
type Entry = tuple[str, int, int, int]
_DIR: Entry = ("dir", 0, 0, 0)


@dataclass(frozen=True)
class Watch:
    """Where one stage may and may not leave changes. `allowed` holds the places inside the
    watched folders that may change during a stage, besides the worktree and the run folder."""

    repo: Path  # the main checkout
    worktree: Path
    run_folder: Path
    watched: tuple[Path, ...]
    allowed: tuple[Path, ...]

    @classmethod
    def for_stage(
        cls, *, repo: Path, data: Path, worktree: Path, run_folder: Path, config_dir: Path
    ) -> "Watch":
        """The data folder watched; the agent's own config folder (its transcripts) and the
        registry (with SQLite's side files) may change."""
        registry = data / REGISTRY_FILE
        sqlite = tuple(registry.with_name(registry.name + s) for s in SQLITE_SUFFIXES)
        return cls(
            repo=repo,
            worktree=worktree,
            run_folder=run_folder,
            watched=(data,),
            allowed=(config_dir, *sqlite),
        )


@dataclass(frozen=True)
class MainState:
    head: str
    index: str  # hash of the index entries
    files: Mapping[str, Entry]  # by repo-relative path


@dataclass(frozen=True)
class Baseline:
    """What the engine saw before the stage started."""

    main: MainState
    watched: Mapping[str, Entry]  # by full path
    run_folder: Mapping[str, Entry]  # by run-folder-relative path
    base: str  # the worktree's commit when the stage started


@dataclass(frozen=True)
class PostStageOutcome:
    run: Run  # moved to needs_you if a check failed
    results: tuple[GateResult, ...]

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)


# ------------------------------------------------------------------ baseline


def take_baseline(git: Git, watch: Watch) -> Baseline:
    """Record the state the stage must leave alone. Take it right before the agent starts."""
    try:
        return Baseline(
            main=_main_state(git, watch),
            watched=_watched_state(watch),
            run_folder=_tree(watch.run_folder.resolve(), frozenset()),
            base=git.out(["rev-parse", "HEAD"], watch.worktree),
        )
    except (OSError, GateError, GitError) as err:
        raise GateError(
            f"can't record the state before the stage ({err}). Fix the folder, then start "
            "the stage again."
        ) from err


def _main_state(git: Git, watch: Watch) -> MainState:
    repo = watch.repo.resolve()
    branch = git.run(["symbolic-ref", "-q", "HEAD"], repo, check=False).stdout.strip()
    commit = git.run(["rev-parse", "-q", "--verify", "HEAD"], repo, check=False).stdout.strip()
    index = git.run(["ls-files", "--stage", "-z"], repo).stdout
    dot_git = repo / ".git"
    skip = _skips(watch)
    files: dict[str, Entry] = {}
    files.update(_tree(repo, skip | {_key(dot_git)} if dot_git.is_dir() else skip))
    for git_dir in _git_dirs(git, repo):
        for name in GIT_WATCHED:
            files.update(_tree(git_dir / name, skip))
    return MainState(
        head=f"{branch or 'detached'} {commit or 'no commit'}",
        index=hashlib.sha256(index.encode("utf-8", "replace")).hexdigest(),
        files={_shown_main(Path(path), repo): entry for path, entry in files.items()},
    )


def _git_dirs(git: Git, repo: Path) -> list[Path]:
    """The checkout's own git folder and the shared one, as git finds them. They differ when the
    checkout is itself a linked worktree (its `.git` is a file pointing elsewhere)."""
    out = git.out(["rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"], repo)
    found: dict[str, Path] = {}
    for line in out.splitlines():
        if line.strip():
            path = Path(line.strip()).resolve()
            found.setdefault(_key(path), path)
    if not found:
        raise GateError(f"git found no git folder for {repo}.")
    return list(found.values())


def _shown_main(path: Path, repo: Path) -> str:
    """Repo-relative inside the checkout; a shared git folder elsewhere keeps its full path."""
    return _rel(path, repo) if path.is_relative_to(repo) else path.as_posix()


def _watched_state(watch: Watch) -> dict[str, Entry]:
    skip = _skips(watch) | {_key(watch.repo.resolve())}
    found: dict[str, Entry] = {}
    for folder in watch.watched:
        found.update(_tree(folder.resolve(), skip))
    return found


def _skips(watch: Watch) -> frozenset[str]:
    places = (watch.worktree, watch.run_folder, *watch.allowed)
    return frozenset(_key(place.resolve()) for place in places)


def _tree(root: Path, skip: frozenset[str]) -> dict[str, Entry]:
    """Every entry under `root` (itself included), by full path, without following links.
    `root` must be resolved; places in `skip` and everything under them are left out."""
    found: dict[str, Entry] = {}
    if _key(root) in skip:
        return found
    try:
        info = root.lstat()
    except FileNotFoundError:
        return found
    found[str(root)] = _entry(info)
    if found[str(root)] != _DIR:
        return found
    folders = [root]
    while folders:
        folder = folders.pop()
        try:
            entries = list(os.scandir(folder))
        except FileNotFoundError:
            continue
        except OSError as err:
            raise GateError(f"can't list {folder} ({err})") from err
        for item in entries:
            path = Path(item.path)
            if _key(path) in skip:
                continue
            try:
                info = item.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as err:
                raise GateError(f"can't read {path} ({err})") from err
            linked = item.is_symlink() or item.is_junction()
            entry = ("link", info.st_size, info.st_mode, info.st_mtime_ns) if linked else None
            found[str(path)] = entry or _entry(info)
            if entry is None and found[str(path)] == _DIR:
                folders.append(path)
    return found


def _entry(info: os.stat_result) -> Entry:
    if stat.S_ISDIR(info.st_mode):
        return _DIR
    kind = "file" if stat.S_ISREG(info.st_mode) else "other"
    return (kind, info.st_size, info.st_mode, info.st_mtime_ns)


# ------------------------------------------------------------------ the checks


def check_stage(
    git: Git,
    watch: Watch,
    baseline: Baseline,
    *,
    role: Role,
    settings: RoleSettings,
    locked: Iterable[str] = (),
) -> list[GateResult]:
    """The four checks, in order. `settings` are the role's merged settings for this run and
    `locked` the files the tester created (worktree-relative). A check that raises fails."""
    try:
        policy: PathPolicy | Exception = PathPolicy.build(
            role, settings, worktree=watch.worktree, run_folder=watch.run_folder, locked=locked
        )
    except Exception as err:
        policy = err
    changed: list[str] | Exception
    try:
        changed = _changed_paths(git, watch.worktree, baseline.base)
    except Exception as err:
        changed = err

    def main() -> GateResult:
        return _check_main(git, watch, baseline, role)

    def outside() -> GateResult:
        return _check_outside(watch, baseline, role, _ready(policy))

    def paths() -> GateResult:
        return _check_paths(role, _ready(policy), _ready(changed))

    def secrets() -> GateResult:
        return _check_secrets(git, watch.worktree, baseline.base, role)

    checks = ((MAIN_CHECKOUT, main), (OUTSIDE_WORKTREE, outside))
    checks += ((ALLOWED_PATHS, paths), (SECRETS, secrets))
    results: list[GateResult] = []
    for gate, run_check in checks:
        try:
            results.append(run_check())
        except Exception as err:  # fail closed
            results.append(
                _fail(
                    gate,
                    f"the check couldn't run after the {role} stage ({err}), so the stage "
                    "can't be trusted. Fix the cause, then decide the run.",
                )
            )
    return results


def _ready[T](value: T | Exception) -> T:
    if isinstance(value, Exception):
        raise value
    return value


def _check_main(git: Git, watch: Watch, baseline: Baseline, role: Role) -> GateResult:
    before, after = baseline.main, _main_state(git, watch)
    problems: list[str] = []
    if before.head != after.head:
        problems.append(f"HEAD moved ({before.head} -> {after.head})")
    if before.index != after.index:
        problems.append("the index changed")
    problems += _differences(before.files, after.files)
    if not problems:
        return _pass(MAIN_CHECKOUT, "the main checkout is unchanged.")
    return _fail(
        MAIN_CHECKOUT,
        f"the main checkout {watch.repo} changed during the {role} stage: "
        f"{_listed(problems)}. Agents work only in their worktree; check what wrote there "
        "and restore it before carrying on.",
    )


def _check_outside(watch: Watch, baseline: Baseline, role: Role, policy: PathPolicy) -> GateResult:
    problems = _differences(baseline.watched, _watched_state(watch))
    own = policy.run_docs | {_case(name, policy.windows) for name in ENGINE_FILES}
    after = _tree(watch.run_folder.resolve(), frozenset())
    root = str(watch.run_folder.resolve())
    for change in _differences(baseline.run_folder, after, root=root):
        name = change.split(" ", 1)[1]
        if _case(name, policy.windows) not in own:
            problems.append(f"{change} in the run folder (not the {role}'s to write)")
    if not problems:
        return _pass(OUTSIDE_WORKTREE, "nothing outside the worktree changed.")
    return _fail(
        OUTSIDE_WORKTREE,
        f"files outside the worktree changed during the {role} stage: {_listed(problems)}. "
        "Something the agent ran wrote outside its worktree; check those files.",
    )


def _check_paths(role: Role, policy: PathPolicy, changed: Sequence[str]) -> GateResult:
    refused: list[str] = []
    for rel in changed:
        decision = check_file_tool(policy, "Write", {"file_path": str(policy.worktree / rel)})
        if not decision.allowed:
            refused.append(f"{rel} ({decision.reason.removeprefix('Blocked: ')})")
    if not refused:
        return _pass(ALLOWED_PATHS, f"the diff touches only the {role}'s allowed paths.")
    return _fail(
        ALLOWED_PATHS,
        f"the {role}'s diff touches paths it may not change: {_listed(refused)}.",
    )


def _check_secrets(git: Git, worktree: Path, base: str, role: Role) -> GateResult:
    diff = git.run(["diff", *DIFF_OPTIONS, base, "--"], worktree).stdout
    findings = scan_diff(diff)
    for rel in _untracked(git, worktree):
        findings += _scan_file(worktree / rel, rel)
    # The push sends every commit, so a secret committed and later deleted counts too.
    commits = git.run(
        ["log", "-p", "--format=", "--diff-merges=first-parent", *DIFF_OPTIONS]
        + _new_commits(base),
        worktree,
    )
    findings += scan_diff(commits.stdout)
    # Each commit read whole and on its own: its message and author are text the author chose,
    # so no byte in them can be trusted to separate one commit from the next.
    for commit, _ in _stage_commits(git, worktree, base):
        raw = git.run(["cat-file", "commit", commit], worktree).stdout
        findings += scan_text(raw, path=f"commit {commit[:12]}")
    if not findings:
        return _pass(SECRETS, "the secret scan of the diff is clean.")
    listed = _listed([finding.describe() for finding in findings])
    return _fail(
        SECRETS,
        f"the {role}'s diff or commits add secrets: {listed}. Secrets never go into the repo; "
        "remove them (a later commit doesn't take one out of the history) and keep test "
        "secrets in the repo's `.env.factory` outside the worktree.",
    )


def _scan_file(path: Path, rel: str) -> list[SecretFinding]:
    """An untracked file as git will commit it: a link is its target text, and a binary file
    (or anything that isn't a file) holds no text to scan."""
    if path.is_symlink():
        return scan_text(str(path.readlink()), path=rel)
    if not path.is_file():
        return []
    data = path.read_bytes()
    if b"\0" in data[:BINARY_PROBE]:
        return []  # binary, as git diff treats it
    return scan_text(data.decode("utf-8", errors="replace"), path=rel)


# ------------------------------------------------------------------ the diff


def _changed_paths(git: Git, worktree: Path, base: str) -> list[str]:
    """Every worktree-relative path the stage added, changed or deleted since `base`, in the
    final files or in any commit made during the stage (even if a later commit put it back)."""
    tracked = git.run(["diff", "--name-only", "--no-renames", "-z", base, "--"], worktree).stdout
    names = set(z_names(tracked))
    names.update(_untracked(git, worktree))
    for commit, parent in _stage_commits(git, worktree, base):
        against = [parent, commit] if parent else ["--root", commit]
        out = git.run(
            ["diff-tree", "-r", "-z", "--name-only", "--no-renames", "--no-commit-id", *against],
            worktree,
        ).stdout
        names.update(z_names(out))
    return sorted(names)


def z_names(out: str) -> list[str]:
    """The names in git's `-z` output, exactly as git wrote them. A name may hold any byte but
    NUL (a newline, spaces, control bytes), so split on NUL only and strip nothing."""
    return [name for name in out.split("\0") if name]


def _new_commits(base: str) -> list[str]:
    """The `git log` range of the commits made during the stage: those on HEAD and not `base`."""
    return [f"{base}..HEAD", "--"]


def _stage_commits(git: Git, worktree: Path, base: str) -> list[tuple[str, str | None]]:
    """(commit, its first parent or None for a root commit) for each commit made during the
    stage. Each line `git rev-list --parents` prints is ids only, so splitting it is exact; a
    merge is judged against its first parent, as the push's diff is."""
    out = git.run(["rev-list", "--parents", *_new_commits(base)], worktree).stdout
    commits: list[tuple[str, str | None]] = []
    for line in out.splitlines():
        ids = line.split()
        if ids:
            commits.append((ids[0], ids[1] if len(ids) > 1 else None))
    return commits


def _untracked(git: Git, worktree: Path) -> list[str]:
    out = git.run(["ls-files", "--others", "--exclude-standard", "-z"], worktree).stdout
    return z_names(out)


# ------------------------------------------------------------------ helpers


def _differences(
    before: Mapping[str, Entry], after: Mapping[str, Entry], *, root: str | None = None
) -> list[str]:
    """ "added x", "removed x" and "changed x" for each entry that differs, sorted. With `root`,
    full paths are shown relative to it."""

    def shown(path: str) -> str:
        return Path(path).relative_to(root).as_posix() if root else path

    out: list[str] = []
    for path in sorted(before.keys() | after.keys()):
        if path not in after:
            out.append(f"removed {shown(path)}")
        elif path not in before:
            out.append(f"added {shown(path)}")
        elif before[path] != after[path]:
            out.append(f"changed {shown(path)}")
    return out


def _listed(items: Sequence[str]) -> str:
    shown = "; ".join(items[:MAX_LISTED])
    more = len(items) - MAX_LISTED
    return f"{shown}; and {more} more" if more > 0 else shown


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix() or "."


def _key(path: Path) -> str:
    return os.path.normcase(str(path))


def _case(name: str, windows: bool) -> str:
    return name.casefold() if windows else name


def _pass(gate: str, reason: str) -> GateResult:
    return GateResult(gate=gate, passed=True, reason=reason)


def _fail(gate: str, reason: str) -> GateResult:
    return GateResult(gate=gate, passed=False, reason=reason)


# ------------------------------------------------------------------ stopping the run


def post_stage_check(
    git: Git,
    watch: Watch,
    baseline: Baseline,
    *,
    role: Role,
    settings: RoleSettings,
    run: Run,
    events: EventLog,
    registry: RunIndex,
    locked: Iterable[str] = (),
) -> PostStageOutcome:
    """Run the checks and log each as a `gate_result` event. Any failure stops the run: it
    moves to needs_you with a `needs_you` event naming the failed checks, and is saved."""
    results = tuple(check_stage(git, watch, baseline, role=role, settings=settings, locked=locked))
    for result in results:
        events.append(result, role=role)
    failed = [result for result in results if not result.passed]
    if not failed:
        return PostStageOutcome(run, results)
    moved = move(run, "needs_you", log=events)
    listed = " ".join(f"{result.gate}: {result.reason}" for result in failed)
    events.append(
        NeedsYou(reason=f"the engine's check after the {role} stage failed. {listed}"),
        role=role,
    )
    persist(moved, watch.run_folder, registry)
    return PostStageOutcome(moved, results)
