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

import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePath

from factory_engine.config import RoleSettings
from factory_engine.run import Role

# Agents never write these, whatever the role's write paths say.
PROTECTED = (".git/**", ".claude/**", ".github/**", "factory.yaml", "CLAUDE.md", "**/.env*")
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
        return cls(
            role=role,
            worktree=worktree.resolve(),
            run_folder=run_folder.resolve(),
            write_paths=tuple(p for p in settings.write_paths if p not in RUN_DOCS),
            deny_paths=tuple(settings.deny_paths),
            run_docs=frozenset(_key(d, windows) for d in docs),
            locked=frozenset(
                _key(p.replace("\\", "/").removeprefix("./"), windows) for p in locked
            ),
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
        return ALLOW  # searches the working folder, the worktree
    if not isinstance(raw, str) or not raw.strip():
        return _block(f"{tool_name} needs a file path in `{path_key}`.")
    located = _locate(raw, policy)
    if isinstance(located, PathDecision):
        return located
    return _write(policy, raw, located) if writes else _read(policy, raw, located)


def _read(policy: PathPolicy, raw: str, path: Path) -> PathDecision:
    rel = _relative(path, policy.worktree, policy.windows)
    if rel is None:
        rel = _relative(path, policy.run_folder, policy.windows)
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
    except (OSError, ValueError) as err:
        return _block(f"{raw} can't be resolved ({err}).")


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
