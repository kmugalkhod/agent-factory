"""Path check for the file tools (Read, Write, Edit, MultiEdit, NotebookEdit, Glob, Grep).

An agent may read anything in its worktree and its run folder, except secrets (`.env*`). It may
write in the worktree only where its role's `write_paths` allow and its `deny_paths` don't, and
never to a protected file. The builder may not change files the tester created (the test lock).
In the run folder a role may write only its own run docs: the run-doc names in its
`write_paths` (`plan.md`, `review.md`) and its `handoff-<role>.md` (not the reviewer's).

Every path is resolved before it is judged, so `..`, junctions, symlinks and 8.3 short names
are judged by where they really lead. Windows also rewrites some names on the way to the disk
(`factory.yaml.` opens `factory.yaml`, `a.py:x` is a stream of `a.py`), so those are refused.
Every block carries a reason the agent can act on.
"""

import os
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePath

from factory_engine.config import RoleSettings
from factory_engine.errors import SafetyError
from factory_engine.run import Role

# Agents never write these, whatever the role's write paths say.
# `.git` alone is a file in a linked worktree.
PROTECTED = (
    ".git",
    ".git/**",
    ".claude/**",
    ".github/**",
    "factory.yaml",
    "CLAUDE.md",
    "**/.env*",
)
SECRETS = ("**/.env*",)
RUN_DOCS = frozenset({"plan.md", "review.md"})
NO_HANDOFF: frozenset[Role] = frozenset({"reviewer"})

# Tool name -> (input key holding the path, writes?, input key holding a glob pattern).
FILE_TOOLS: dict[str, tuple[str, bool, str | None]] = {
    "Read": ("file_path", False, None),
    "Write": ("file_path", True, None),
    "Edit": ("file_path", True, None),
    "MultiEdit": ("file_path", True, None),
    "NotebookEdit": ("notebook_path", True, None),
    "Glob": ("path", False, "pattern"),
    "Grep": ("path", False, "glob"),
}
OPTIONAL_PATH = frozenset({"Glob", "Grep"})  # these search the worktree when no path is given
SEARCHES_CONTENT = frozenset({"Grep"})  # reads every file it searches, so each one is checked
UNSURE_GLOB = re.compile(r"[{}\[\]]|^!")  # braces, classes and negation: assume it matches


@dataclass(frozen=True)
class PathDecision:
    allowed: bool
    reason: str = ""


ALLOW = PathDecision(True)


def _block(reason: str) -> PathDecision:
    return PathDecision(False, f"Blocked: {reason}")


@dataclass(frozen=True)
class PathPolicy:
    """What one role may touch in one run. Build it with `PathPolicy.build`."""

    role: Role
    worktree: Path
    run_folder: Path
    write_paths: tuple[str, ...]
    deny_paths: tuple[str, ...]
    run_docs: frozenset[str]
    locked: frozenset[str]
    windows: bool

    @classmethod
    def build(
        cls,
        role: Role,
        settings: RoleSettings,
        *,
        worktree: Path,
        run_folder: Path,
        locked: Iterable[str] = (),
        windows: bool = sys.platform == "win32",
    ) -> "PathPolicy":
        """`locked` holds the worktree-relative paths of the files the tester created."""
        docs = {p for p in settings.write_paths if p in RUN_DOCS}
        if role not in NO_HANDOFF:
            docs.add(f"handoff-{role}.md")
        root = worktree.resolve()
        return cls(
            role=role,
            worktree=root,
            run_folder=run_folder.resolve(),
            write_paths=tuple(p for p in settings.write_paths if p not in RUN_DOCS),
            deny_paths=tuple(settings.deny_paths),
            run_docs=frozenset(_key(d, windows) for d in docs),
            locked=frozenset(_key(_locked_entry(root, p, windows), windows) for p in locked),
            windows=windows,
        )


def check_file_tool(
    policy: PathPolicy, tool_name: str, tool_input: Mapping[str, object]
) -> PathDecision:
    """Allow or block one file-tool call."""
    if tool_name not in FILE_TOOLS:
        return _block(f"{tool_name} is not a file tool; the path check can't judge it.")
    path_key, writes, pattern_key = FILE_TOOLS[tool_name]
    if pattern_key is not None and pattern_key in tool_input:
        bad = _bad_pattern(tool_input[pattern_key])
        if bad:
            return bad
    raw = tool_input.get(path_key)
    if raw is None and tool_name in OPTIONAL_PATH:
        raw, located = ".", policy.worktree  # searches the working folder, the worktree
    elif not isinstance(raw, str) or not raw.strip():
        return _block(f"{tool_name} needs a file path in `{path_key}`.")
    else:
        located = _locate(raw, policy)
        if isinstance(located, PathDecision):
            return located
    if writes:
        return _write(policy, raw, located)
    decision = _read(policy, raw, located)
    if decision.allowed and tool_name in SEARCHES_CONTENT:
        glob = tool_input.get(pattern_key) if pattern_key else None
        return _search(policy, located, glob if isinstance(glob, str) else None)
    return decision


def _search(policy: PathPolicy, root: Path, glob: str | None) -> PathDecision:
    """Check every file a content search under `root` can open: no secrets files, and no
    link that leads out of the working folders. A glob the check can't judge counts as
    matching everything."""
    if not root.is_dir():
        return ALLOW  # one file, already checked as a read
    folders = [root]
    while folders:
        folder = folders.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError as err:
            return _block(f"can't list {folder} to check the search ({err}); narrow `path`.")
        for entry in entries:
            path = Path(entry.path)
            if entry.is_symlink() or entry.is_junction():
                try:
                    target = path.resolve()
                except (OSError, ValueError, RuntimeError) as err:
                    return _block(
                        f"the search meets {path}, a link that can't be resolved ({err})."
                    )
                rel = path.relative_to(root).as_posix()
                blocked = _link_decision(policy, path, rel, target, target.is_file(), glob)
                if blocked:
                    return blocked
                continue  # a linked folder inside is searched where it really lives
            if entry.is_dir(follow_symlinks=False):
                folders.append(path)
            elif _matches(entry.name, SECRETS, policy.windows) and _glob_may_match(
                glob, path.relative_to(root).as_posix(), entry.name, policy.windows
            ):
                return _block(
                    f"the search would read {path}, a secrets file (.env*). Narrow it with "
                    "`glob` or `path` so it skips the file."
                )
    return ALLOW


def _link_decision(
    policy: PathPolicy, link: Path, rel: str, target: Path, is_file: bool, glob: str | None
) -> PathDecision | None:
    """A block for a link the search can open, or None. A linked file the glob skips is never
    opened; a linked folder can't be judged by the glob, which filters the files inside it."""
    if is_file and not _glob_may_match(glob, rel, link.name, policy.windows):
        return None
    if _working_relative(policy, target) is None:
        return _block(
            f"the search would follow {link}, a link outside the worktree. Narrow `path` or "
            "`glob` to skip it."
        )
    if is_file and (
        _matches(link.name, SECRETS, policy.windows)
        or _matches(target.name, SECRETS, policy.windows)
    ):
        return _block(
            f"the search would read {link}, a link to a secrets file (.env*). Narrow it with "
            "`glob` or `path` so it skips the file."
        )
    return None


def _glob_may_match(glob: str | None, rel: str, name: str, windows: bool) -> bool:
    """Whether a search glob can select a file. Like ripgrep, a glob without `/` matches the
    file name, one with `/` the path under the search folder."""
    if glob is None or UNSURE_GLOB.search(glob):
        return True
    target = rel if "/" in glob.replace("\\", "/") else name
    return _matches(target, (glob.replace("\\", "/").removeprefix("./"),), windows)


def _working_relative(policy: PathPolicy, path: Path) -> str | None:
    rel = _relative(path, policy.worktree, policy.windows)
    return rel if rel is not None else _relative(path, policy.run_folder, policy.windows)


def _read(policy: PathPolicy, raw: str, path: Path) -> PathDecision:
    rel = _working_relative(policy, path)
    if rel is None:
        return _outside(policy, raw)
    if _matches(rel, SECRETS, policy.windows):
        return _block(f"{raw} is a secrets file (.env*); agents never read them.")
    return ALLOW


def _write(policy: PathPolicy, raw: str, path: Path) -> PathDecision:
    role = policy.role
    rel = _relative(path, policy.run_folder, policy.windows)
    if rel is not None:
        if _key(rel, policy.windows) in policy.run_docs:
            return ALLOW
        allowed = ", ".join(sorted(policy.run_docs)) or "nothing"
        return _block(f"in the run folder the {role} may write only: {allowed}. Not {raw}.")
    rel = _relative(path, policy.worktree, policy.windows)
    if rel is None:
        return _outside(policy, raw)
    if not rel:
        return _block(f"{raw} is the worktree itself; write a file inside it.")
    if _matches(rel, PROTECTED, policy.windows):
        return _block(f"{rel} is a protected file; agents never change it.")
    if role != "tester" and _key(rel, policy.windows) in policy.locked:
        return _block(
            f"{rel} was written by the tester and is locked. Make the tests pass by changing "
            "the code, not the tests; if a test is wrong, say so in your handoff."
        )
    denied = next((p for p in policy.deny_paths if _matches(rel, (p,), policy.windows)), None)
    if denied:
        return _block(f"{rel} is in the {role}'s denied paths ({denied}).")
    if not _matches(rel, policy.write_paths, policy.windows):
        allowed = ", ".join(policy.write_paths) or "nothing"
        return _block(f"the {role} may write only {allowed} in the worktree. Not {rel}.")
    return ALLOW


def _outside(policy: PathPolicy, raw: str) -> PathDecision:
    return _block(
        f"{raw} is outside the worktree ({policy.worktree}) and the run folder "
        f"({policy.run_folder}). Work only inside them."
    )


def _locate(raw: str, policy: PathPolicy) -> Path | PathDecision:
    """The real path a tool call would touch, or a block for a path that can't be judged."""
    if raw.startswith("~"):
        return _block(f"{raw} points into the home folder; use a path inside the worktree.")
    if raw.replace("\\", "/").startswith("//"):
        return _block(f"{raw} is a network or device path; use a path inside the worktree.")
    path = Path(raw)
    if not path.is_absolute() and (path.drive or path.root):
        return _block(
            f"{raw} is ambiguous; give a full path with a drive letter or a path relative "
            "to the worktree."
        )
    if policy.windows:
        names = path.parts[1:] if path.anchor else path.parts
        if any(":" in name for name in names):
            return _block(f"{raw} names a data stream (`:`); write the file itself.")
        if any(name not in (".", "..") and name.endswith((".", " ")) for name in names):
            return _block(f"{raw} has a name with a trailing dot or space; Windows drops them.")
    try:
        return (path if path.is_absolute() else policy.worktree / path).resolve()
    except (OSError, ValueError, RuntimeError) as err:  # RuntimeError: a symlink loop
        return _block(f"{raw} can't be resolved ({err}).")


def _locked_entry(worktree: Path, entry: str, windows: bool) -> str:
    """A locked file as its real path under the (resolved) worktree, so aliases match."""
    try:
        rel = _relative((worktree / entry).resolve(), worktree, windows)
    except (OSError, ValueError, RuntimeError) as err:
        raise SafetyError(f"locked file {entry} can't be resolved ({err}).") from err
    if not rel:
        raise SafetyError(
            f"locked file {entry} is not a file inside the worktree ({worktree}); pass the "
            "tester's files as paths relative to the worktree."
        )
    return rel


def _bad_pattern(pattern: object) -> PathDecision | None:
    if not isinstance(pattern, str):
        return _block("the glob pattern must be text.")
    pure = PurePath(pattern)
    if pattern.startswith(("/", "\\", "~")) or pure.drive or ":" in pattern:
        return _block(f"glob pattern {pattern} must be relative to the search folder.")
    if ".." in re.split(r"[\\/]", pattern):
        return _block(f"glob pattern {pattern} uses `..`; search inside the worktree only.")
    return None


def _relative(path: Path, root: Path, windows: bool) -> str | None:
    """`path` relative to `root` as `a/b`, or None when it isn't inside. "" is `root` itself."""
    parts, root_parts = path.parts, root.parts
    if len(parts) < len(root_parts):
        return None
    if [_key(p, windows) for p in parts[: len(root_parts)]] != [
        _key(p, windows) for p in root_parts
    ]:
        return None
    return "/".join(parts[len(root_parts) :])


def _key(text: str, windows: bool) -> str:
    return text.casefold() if windows else text


def _matches(rel: str, patterns: Iterable[str], windows: bool) -> bool:
    flags = re.IGNORECASE if windows else 0
    return any(re.fullmatch(_regex(p), rel, flags) for p in patterns)


def _regex(pattern: str) -> str:
    """A glob as a regex: `**` spans folders (or none), `*` and `?` stay within one name."""
    parts = pattern.split("/")
    out: list[str] = []
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        if part == "**":
            out.append(".*" if last else "(?:[^/]+/)*")
            continue
        out.append("".join(_char(c) for c in part) + ("" if last else "/"))
    return "".join(out)


def _char(c: str) -> str:
    return "[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c)
