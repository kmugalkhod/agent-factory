"""Shell check for the Bash and PowerShell tools.

A command is split into its parts at `&&`, `||`, `;`, `|`, `&`, newlines and parentheses, with
quotes removed the way the shell would remove them, and every part is judged on its own:

- No role ever runs `git` (or `gh`): the engine commits, pushes and opens pull requests.
- Nothing reaches the network: `curl`, `wget`, `Invoke-WebRequest`, `iwr`, `irm` and the like.
- `cd` stays inside the worktree; a delete never targets the worktree, a parent of it or
  anything outside it.
- Otherwise a part runs only if it starts with one of the role's allowlisted commands (from
  `factory.yaml`), or, for a read-only role such as the planner, is a read-only command whose
  paths stay inside the worktree and the run folder and whose reads never reach `.env*` files.
- A role with no shell (the reviewer) is denied every command.

Whatever the check can't judge is blocked: variable and command expansion (`$X`, `$(...)`,
backticks, `%X%`, `@splat`), brace expansion, input redirects and here-docs, output redirects
to anything but the null device, `VAR=x` prefixes, and wrappers that run another command
(`cmd /c`, `powershell -c`, `bash -c`, `env`, `xargs`, `iex`, ...). A wrapper's inner command is
still checked, so its block names the real problem.
"""

import glob
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from factory_engine.config import Settings
from factory_engine.run import Role

Shell = Literal["bash", "powershell"]
SHELL_TOOLS: dict[str, Shell] = {"Bash": "bash", "PowerShell": "powershell"}

GIT = frozenset({"git", "gitk", "git-bash", "git-cmd", "gh", "hub"})
NETWORK = frozenset(
    {
        "curl",
        "wget",
        "invoke-webrequest",
        "iwr",
        "invoke-restmethod",
        "irm",
        "start-bitstransfer",
        "bitsadmin",
    }
)
CD = frozenset({"cd", "chdir", "set-location", "sl", "pushd", "push-location"})
DELETE = frozenset({"rm", "rmdir", "rd", "del", "erase", "remove-item", "ri", "unlink"})
# Shells: name -> the flags that take an inline command, and the shell that reads it.
SHELLS: dict[str, tuple[frozenset[str], Shell]] = {
    "cmd": (frozenset({"/c", "/k", "/r"}), "powershell"),
    "powershell": (frozenset({"-c", "-command"}), "powershell"),
    "pwsh": (frozenset({"-c", "-command"}), "powershell"),
    "bash": (frozenset({"-c"}), "bash"),
    "sh": (frozenset({"-c"}), "bash"),
    "zsh": (frozenset({"-c"}), "bash"),
}
ENCODED = ("-e", "-ec", "-en", "-enc", "-encodedcommand")
# Commands that run the rest of their arguments (or a string) as another command.
WRAPPERS = frozenset(
    {
        "env",
        "xargs",
        "nohup",
        "timeout",
        "time",
        "nice",
        "sudo",
        "runas",
        "exec",
        "eval",
        "source",
        ".",
        "call",
        "start",
        "start-process",
        "saps",
        "invoke-expression",
        "iex",
        "invoke-command",
        "icm",
        "wsl",
    }
)
# Read-only commands for a read-only role, with the flags that would write or run something.
READ_ONLY: dict[str, frozenset[str]] = {
    name: frozenset(flags)
    for name, flags in {
        "ls": (),
        "dir": (),
        "pwd": (),
        "cat": (),
        "type": (),
        "head": (),
        "tail": (),
        "wc": (),
        "grep": (),
        "rg": ("--pre", "--pre-glob", "-z", "--search-zip", "--hostname-bin"),
        "find": ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0")
        + ("-fprintf", "-fls"),
        "tree": ("-o",),
        "stat": (),
        "file": (),
        "du": (),
        "diff": (),
        "sort": ("-o", "--output", "--compress-program"),
        "which": (),
        "where": (),
        "get-childitem": (),
        "gci": (),
        "get-content": (),
        "gc": (),
        "select-string": (),
        "sls": (),
        "get-location": (),
        "gl": (),
        "get-item": (),
        "gi": (),
        "test-path": (),
        "resolve-path": (),
        "rvpa": (),
        "measure-object": (),
        "measure": (),
        "select-object": (),
        "sort-object": (),
    }.items()
}
# Read-only commands that read file contents, so a folder they get may not hold secrets.
READS_CONTENT = frozenset(
    {"cat", "type", "head", "tail", "wc", "grep", "rg", "diff", "sort"}
    | {"get-content", "gc", "select-string", "sls"}
)
# Searches: the first positional argument is the pattern, and no path means the current folder.
SEARCHES = frozenset({"grep", "rg", "select-string", "sls"})
# In PowerShell these names are cmdlet aliases (`sort` is Sort-Object), whose parameters are
# whole words, not combinable short flags.
CMDLET_ALIASES = frozenset({"sort"})
SUFFIXES = (".exe", ".cmd", ".bat", ".com", ".ps1")
WILDCARDS = "*?["
OPERATORS = frozenset({";", "&&", "||", "|", "&"})

_EXPANSION = (
    "variable and command expansion ($X, $(...), backticks, %X%, @splat) isn't allowed; "
    "write the values out."
)


@dataclass(frozen=True)
class CommandDecision:
    allowed: bool
    reason: str = ""


ALLOW = CommandDecision(True)


class _Blocked(Exception):
    """Raised inside the check; turned into a blocked decision at the top."""


@dataclass(frozen=True)
class ShellPolicy:
    """What one role may run in one run. Build it with `ShellPolicy.build`."""

    role: Role
    worktree: Path
    run_folder: Path
    allowlist: tuple[str, ...]  # configured commands, parsed with each call's own shell rules
    read_only: bool
    windows: bool

    @classmethod
    def build(
        cls,
        role: Role,
        settings: Settings,
        *,
        worktree: Path,
        run_folder: Path,
        windows: bool = sys.platform == "win32",
    ) -> "ShellPolicy":
        role_settings = getattr(settings.roles, role)
        commands: list[str] = []
        for name in role_settings.shell_allowlist:
            command: str | None = getattr(settings.commands, name)
            if command:  # a command that isn't configured allows nothing
                commands.append(command)
        return cls(
            role=role,
            worktree=worktree.resolve(),
            run_folder=run_folder.resolve(),
            allowlist=tuple(commands),
            read_only=role_settings.shell_read_only,
            windows=windows,
        )

    @property
    def has_shell(self) -> bool:
        return self.read_only or bool(self.allowlist)


def check_shell_command(
    policy: ShellPolicy,
    tool_name: str,
    tool_input: Mapping[str, object],
    *,
    cwd: Path | None = None,
) -> CommandDecision:
    """Allow or block one Bash or PowerShell call. `cwd` is the shell's folder (the worktree
    when not given)."""
    shell = SHELL_TOOLS.get(tool_name)
    if shell is None:
        return _block(f"{tool_name} isn't a shell tool; the command check can't judge it.")
    if not policy.has_shell:
        return _block(f"the {policy.role} has no shell. Use the file tools to read the code.")
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return _block(f"{tool_name} needs a command in `command`.")
    start = (cwd or policy.worktree).resolve()
    if _relative(policy, start, policy.worktree) is None:
        return _block(f"the shell is in {start}, outside the worktree ({policy.worktree}).")
    try:
        _check(policy, command, shell, {start})
    except _Blocked as blocked:
        return _block(str(blocked))
    return ALLOW


def _block(reason: str) -> CommandDecision:
    return CommandDecision(False, f"Blocked: {reason}")


# ------------------------------------------------------------------ parts


@dataclass
class _Part:
    """One simple command: its words with quotes removed, which of them the shell expands as
    wildcards, whether a pipe feeds it, and whether it runs in its own process (`|`, `&`)."""

    words: list[str]
    wild: list[bool]
    piped: bool
    detached: bool = False


def _check(policy: ShellPolicy, command: str, shell: Shell, cwds: set[Path]) -> None:
    items = _lex(command, shell, policy.windows)
    _, _, end = _evaluate(policy, items, 0, shell, set(cwds))
    if end != len(items):
        raise _Blocked("the parentheses don't match.")


def _evaluate(
    policy: ShellPolicy, items: list[_Part | str], i: int, shell: Shell, start: set[Path]
) -> tuple[set[Path], set[Path], int]:
    """Check the commands from `items[i]` up to a closing `)`. Returns the folders the shell
    can be in when the list succeeds and when it fails, and where the list ended.

    Folders follow the operators: in `a && b`, b runs from where a succeeded; in `a || b`, from
    where a failed; after `;`, from both. A part in its own process (`|`, `&`) can't move the
    shell, and neither can a Bash subshell `( )` (a PowerShell group can). A part that may
    never run is still checked, from every folder known so far."""
    ok, failed = set(start), set[Path]()
    op = ";"
    while i < len(items):
        item = items[i]
        if item == ")":
            return ok, failed, i
        if isinstance(item, str) and item in OPERATORS:
            op = item
            i += 1
            continue
        runs = ok if op == "&&" else failed if op == "||" else ok | failed
        runs = runs or ok | failed
        if isinstance(item, _Part):
            after_ok, after_failed = _check_part(policy, item, shell, runs)
            if item.piped or item.detached:
                after_ok, after_failed = after_ok | runs, after_failed | runs
        else:  # "(": a group, up to its ")"
            inner_ok, inner_failed, i = _evaluate(policy, items, i + 1, shell, runs)
            if i >= len(items):
                raise _Blocked("the parentheses don't match.")
            after_ok = set(runs) if shell == "bash" else inner_ok | runs
            after_failed = set(runs) if shell == "bash" else inner_failed | runs
        if op == "&&":
            ok, failed = after_ok, after_failed | failed
        elif op == "||":
            ok, failed = after_ok | ok, after_failed
        else:
            ok, failed = after_ok, after_failed
        op = ";"
        i += 1
    return ok, failed, i


def _check_part(
    policy: ShellPolicy, part: _Part, shell: Shell, cwds: set[Path]
) -> tuple[set[Path], set[Path]]:
    """Check one part. Returns the folders the shell is in after it succeeds and fails."""
    words = part.words
    if re.match(r"[A-Za-z_]\w*=", words[0]):
        raise _Blocked(f"setting variables ({words[0]}) isn't allowed; run the command plainly.")
    name = _name(words[0])
    args = words[1:]
    if name in GIT or name.startswith("git-"):
        raise _Blocked(
            f"agents never run git (`{words[0]}`); the engine commits, pushes and opens pull "
            "requests. Say what you need in your handoff."
        )
    if name in NETWORK:
        raise _Blocked(f"`{words[0]}` reaches the network; agents don't download anything.")
    if name in SHELLS or name in WRAPPERS:
        _check_wrapper(policy, name, words, shell, cwds)
    if name in CD:
        return _cd(policy, part, shell, cwds)
    if name in DELETE:
        _check_delete(policy, words, args, shell, cwds)
    if _allowlisted(policy, words, shell):
        return cwds, cwds
    if policy.read_only and name in READ_ONLY:
        trusted = _trusted_read_only(policy, words[0], shell)
        if trusted is None:
            raise _Blocked(
                f"`{words[0]}` runs a program from a path; run `{name}` by its plain name."
            )
        _check_read_only(policy, trusted, part, shell, cwds)
        return cwds, cwds
    raise _Blocked(f"`{words[0]}` isn't allowed for the {policy.role}. {_may_run(policy)}")


def _may_run(policy: ShellPolicy) -> str:
    runs = [f"`{c}`" for c in policy.allowlist]
    if policy.read_only:
        runs.append("read-only commands (ls, cat, head, grep, find, ...)")
    return f"It may run only: {', '.join(runs)}."


def _allowlisted(policy: ShellPolicy, words: list[str], shell: Shell) -> bool:
    """Whether the part starts with a configured command, read with this call's shell rules.
    The program must be named exactly as configured: `./uv` is not `uv`."""
    for configured in policy.allowlist:
        try:
            parts = [p for p in _lex(configured, shell, policy.windows) if isinstance(p, _Part)]
        except _Blocked:
            continue  # a configured command this shell can't read allows nothing here
        if len(parts) != 1:
            continue
        allowed = parts[0].words
        if (
            len(words) >= len(allowed)
            and _program(policy, words[0], shell) == _program(policy, allowed[0], shell)
            and words[1 : len(allowed)] == allowed[1:]
        ):
            return True
    return False


def _program(policy: ShellPolicy, word: str, shell: Shell) -> str:
    """A program word as the shell would look it up: case-insensitive on Windows and in
    PowerShell, with `.exe` optional on Windows."""
    if policy.windows or shell == "powershell":
        word = word.casefold()
    if policy.windows and word.endswith(".exe"):
        word = word[: -len(".exe")]
    return word


def _trusted_read_only(policy: ShellPolicy, word: str, shell: Shell) -> str | None:
    """The read-only command a word runs, or None when it names a program by path (`./cat`,
    `.\\cat.ps1`), which could be anything in the repo."""
    if "/" in word or "\\" in word:
        return None
    program = _program(policy, word, shell)
    return program if program in READ_ONLY else None


def _check_wrapper(
    policy: ShellPolicy, name: str, words: list[str], shell: Shell, cwds: set[Path]
) -> None:
    flags = [w.casefold() for w in words[1:]]
    if name in SHELLS:
        if name in ("powershell", "pwsh") and any(f.startswith(ENCODED) for f in flags):
            raise _Blocked(
                f"`{words[0]}` with an encoded command can't be checked; run it plainly."
            )
        inline, inner_shell = SHELLS[name]
        at = next((i for i, f in enumerate(flags) if f in inline), None)
        inner = " ".join(words[at + 2 :]) if at is not None else ""
    else:
        inner, inner_shell = " ".join(w for w in words[1:] if not w.startswith("-")), shell
    if inner.strip():
        _check(policy, inner, inner_shell, set(cwds))
    raise _Blocked(f"`{words[0]}` runs another command; run the command directly.")


# --------------------------------------------------------- cd and deletes


def _cd(
    policy: ShellPolicy, part: _Part, shell: Shell, cwds: set[Path]
) -> tuple[set[Path], set[Path]]:
    """Returns the folders after the cd succeeds and after it fails. Shells apply `..` to the
    folder as typed (`cd link; cd ..` goes back up the link), while `cd -P` and programs use
    the real folder, so both are kept and both must stay inside the worktree."""
    words = part.words
    targets = [(a, w) for a, w in zip(words[1:], part.wild[1:], strict=True) if a[:1] != "-"]
    if not targets:
        raise _Blocked(
            f"`{' '.join(words)}` goes to the home folder; cd only to folders inside the "
            f"worktree ({policy.worktree})."
        )
    leaves = _Blocked(
        f"`{' '.join(words)}` leaves the worktree ({policy.worktree}); cd only to folders "
        "inside it."
    )
    moved: set[Path] = set()
    stays: set[Path] = set()
    raw, wild = targets[0]
    for cwd in cwds:  # every folder the shell could be in at this point
        texts = _expand(policy, raw, wild, cwd, shell)
        if texts is None:
            raise leaves
        for text in texts:
            found = _candidates(text, cwd)
            if found is None or any(_relative(policy, f, policy.worktree) is None for f in found):
                raise leaves
            physical = found[-1]
            moved |= {Path(os.path.normpath(cwd / text)), physical}
            if not physical.is_dir():
                stays.add(cwd)  # the cd fails and the shell stays where it was
    return moved, stays


def _check_delete(
    policy: ShellPolicy, words: list[str], args: list[str], shell: Shell, cwds: set[Path]
) -> None:
    for target in (a for a in args if not a.startswith("-")):
        for cwd in cwds:
            for _, path in _targets(policy, target, True, cwd, shell):
                rel = None if path is None else _relative(policy, path, policy.worktree)
                if not rel:  # outside, the worktree itself, or a parent of it
                    raise _Blocked(
                        f"`{' '.join(words)}` deletes the worktree, a parent folder or "
                        "something outside the worktree."
                    )


# --------------------------------------------------------------- read-only


def _check_read_only(
    policy: ShellPolicy, name: str, part: _Part, shell: Shell, cwds: set[Path]
) -> None:
    words = part.words
    args = list(zip(words[1:], part.wild[1:], strict=True))
    clusters = not (shell == "powershell" and name in CMDLET_ALIASES)
    for arg, _ in args:
        if _forbidden(name, arg, clusters):
            raise _Blocked(
                f"`{words[0]} {arg}` can change files or run commands; the {policy.role} may run "
                "read-only commands only."
            )
    positional = [(a, w) for a, w in args if not a.startswith("-")]
    # Which argument a search takes as its pattern depends on its options (`-e x`, `-A 3`,
    # `-Path x`), so none is skipped: every one is checked as a path, and the current folder too
    # unless input is piped in. A pattern checked as a path is harmless.
    searches_here = name in SEARCHES and not part.piped
    in_options = [
        (value, any(c in value for c in WILDCARDS))
        for arg, _ in args
        if arg.startswith("-")
        for value in _option_values(arg)
    ]
    for cwd in cwds:
        checks: list[tuple[str, Path | None]] = []
        for raw, wild in positional + in_options:
            checks += _targets(policy, raw, wild, cwd, shell)
        if searches_here:  # last, so a named path's own problem is the one reported
            checks += _targets(policy, ".", False, cwd, shell)
        for raw, path in checks:
            if path is None or _working_relative(policy, path) is None:
                raise _Blocked(
                    f"`{words[0]}` reads {raw}, outside the worktree and the run folder."
                )
            if name in READS_CONTENT:
                _check_reads(policy, words[0], path)


def _forbidden(name: str, arg: str, clusters: bool) -> bool:
    """Whether an argument is one of the command's forbidden flags, including attached values
    (`--output=x`, `-ox`) and, with `clusters`, short flags combined in one word (`-uo x`)."""
    option = arg.split("=", 1)[0].casefold()
    for flag in READ_ONLY[name]:
        if option == flag:
            return True
        if flag.startswith("--") and len(option) > 2 and flag.startswith(option):
            return True  # GNU tools accept any unambiguous prefix: --compress-prog
        if clusters and len(flag) == 2 and re.match(r"-[^-]", arg) and flag[1] in arg[1:]:
            return True
    return False


def _option_values(arg: str) -> list[str]:
    """Everything in an option word that could name a file: `--from-file=x`, `-Path:x`, and
    the attached value of a short option (`-fx`)."""
    body = arg.lstrip("-")
    values = [body.split(sep, 1)[1] for sep in "=:" if sep in body]
    if not arg.startswith("--") and len(body) > 1:
        values.append(body[1:])
    return [v for v in values if v]


def _check_reads(policy: ShellPolicy, command: str, path: Path) -> None:
    """Block a content read that can reach a `.env*` file, or follow a link out of the working
    folders. Links are followed, as `grep -R` and friends do."""
    if _is_secret(path.name, policy.windows):
        raise _Blocked(f"`{command}` would read {path}, a secrets file (.env*).")
    if not path.is_dir():
        return
    folders, seen = [path], set[str]()
    while folders:
        folder = folders.pop()
        key = _key(str(folder), policy.windows)
        if key in seen:
            continue
        seen.add(key)
        try:
            entries = list(os.scandir(folder))
        except OSError as err:
            raise _Blocked(f"can't list {folder} to check for secrets ({err}).") from err
        for entry in entries:
            found = Path(entry.path)
            if _is_secret(entry.name, policy.windows):
                raise _Blocked(
                    f"`{command}` would read {found}, a secrets file (.env*). Narrow the paths "
                    "so it skips the file."
                )
            # DirEntry.is_junction is new in Python 3.12, the version this package needs.
            if entry.is_symlink() or entry.is_junction():
                try:
                    target = found.resolve()
                except (OSError, ValueError, RuntimeError) as err:
                    raise _Blocked(f"`{command}` meets {found}, a broken link ({err}).") from err
                if _working_relative(policy, target) is None:
                    raise _Blocked(
                        f"`{command}` could follow {found}, a link outside the worktree and the "
                        "run folder. Narrow the paths to skip it."
                    )
                if target.is_dir():
                    folders.append(target)
                elif _is_secret(target.name, policy.windows):
                    raise _Blocked(
                        f"`{command}` would read {found}, a link to a secrets file (.env*)."
                    )
            elif entry.is_dir(follow_symlinks=False):
                folders.append(found)


def _is_secret(name: str, windows: bool) -> bool:
    return (name.casefold() if windows else name).startswith(".env")


# ----------------------------------------------------------------- paths


def _targets(
    policy: ShellPolicy, raw: str, wild: bool, cwd: Path, shell: Shell
) -> list[tuple[str, Path | None]]:
    """The real paths an argument can name: every match of a wildcard the shell (or a
    PowerShell cmdlet) expands, else the literal path; each from the folder as typed and from
    its real location. None for a path that can't be judged."""
    texts = _expand(policy, raw, wild, cwd, shell)
    if texts is None:
        return [(raw, None)]
    out: list[tuple[str, Path | None]] = []
    for text in texts:
        found = _candidates(text, cwd)
        out += [(text, None)] if found is None else [(text, f) for f in found]
    return out


def _expand(policy: ShellPolicy, raw: str, wild: bool, cwd: Path, shell: Shell) -> list[str] | None:
    """The native paths an argument names: a wildcard's matches, else the argument itself."""
    text = _translate(policy, raw, shell)
    if text is None:
        return None
    if wild and any(c in text for c in WILDCARDS):
        try:
            # Path.glob refuses absolute patterns and `..`, which are exactly the ones to check.
            matches = glob.glob(text, root_dir=cwd, recursive=True, include_hidden=True)  # noqa: PTH207
        except (OSError, ValueError):
            return None
        if matches:
            return matches
    return [text]


def _translate(policy: ShellPolicy, raw: str, shell: Shell) -> str | None:
    """A path argument as a native path, or None when it can't be judged."""
    if raw.startswith("~") or raw.replace("\\", "/").startswith("//"):
        return None  # home folder, network or device path
    if policy.windows and shell == "bash":
        drive = re.match(r"/([A-Za-z])(/|$)", raw)
        if drive:  # Git Bash writes C:\x as /c/x
            return f"{drive.group(1)}:/{raw[drive.end() :]}"
    return raw


def _candidates(text: str, cwd: Path) -> list[Path] | None:
    """Where a path leads from `cwd`, a folder as the shell has it (links not resolved): with
    `..` applied to the folder as typed, and to its real location. Shells and programs differ,
    so both count; the real one is last. None for a path that can't be judged."""
    path = Path(text)
    if not path.is_absolute() and (path.drive or path.root):
        return None  # drive-relative (`C:x`) or rooted without a drive (`\x`, `/x` on Windows)
    try:
        as_typed = Path(os.path.normpath(cwd / path)).resolve()
        real = (cwd.resolve() / path).resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    return [real] if as_typed == real else [as_typed, real]


def _working_relative(policy: ShellPolicy, path: Path) -> str | None:
    rel = _relative(policy, path, policy.worktree)
    return rel if rel is not None else _relative(policy, path, policy.run_folder)


def _relative(policy: ShellPolicy, path: Path, root: Path) -> str | None:
    """`path` relative to `root` as `a/b`, or None when it isn't inside. "" is `root` itself."""
    parts, root_parts = path.parts, root.parts
    if len(parts) < len(root_parts):
        return None
    if [_key(p, policy.windows) for p in parts[: len(root_parts)]] != [
        _key(p, policy.windows) for p in root_parts
    ]:
        return None
    return "/".join(parts[len(root_parts) :])


def _key(text: str, windows: bool) -> str:
    return text.casefold() if windows else text


def _name(word: str) -> str:
    """A command word as a bare, lower-case program name: `C:\\Git\\bin\\Git.EXE` is `git`.
    Used to deny; granting compares the word itself (see `_program`)."""
    base = re.split(r"[\\/]", word)[-1].casefold()
    for suffix in SUFFIXES:
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


# ----------------------------------------------------------------- lexer


def _lex(command: str, shell: Shell, windows: bool) -> list[_Part | str]:
    """Split a command into parts, with `(` and `)` marking groups. Raises `_Blocked` for
    anything the check can't judge."""
    if re.search(r"%[A-Za-z_][\w()]*%", command):
        raise _Blocked(_EXPANSION)
    lexer = _Lexer(command, shell, windows)
    lexer.run()
    return lexer.items


class _Lexer:
    def __init__(self, command: str, shell: Shell, windows: bool) -> None:
        self.text = command
        self.shell = shell
        self.windows = windows
        self.items: list[_Part | str] = []
        self.words: list[str] = []
        self.wild: list[bool] = []
        self.buf: list[str] = []
        self.started = False  # a word has begun, even an empty quoted one
        self.globbed = False  # the word has a wildcard the shell expands
        self.literal = False  # part of the word was quoted or escaped
        self.redirect = False  # the next word is a redirect target
        self.piped = False  # the part being read is fed by a pipe
        self.i = 0

    def run(self) -> None:
        text, n = self.text, len(self.text)
        while self.i < n:
            c = text[self.i]
            if c == "'":
                self._single_quoted()
            elif c == '"':
                self._double_quoted()
            elif c == "`":
                raise _Blocked(
                    "backtick escapes aren't allowed; write the command plainly."
                    if self.shell == "powershell"
                    else _EXPANSION
                )
            elif c == "$":
                self._dollar()
            elif c == "@" and not self.started and self.shell == "powershell":
                raise _Blocked(_EXPANSION)  # @splat and @(...)
            elif c == "\\" and self.shell == "bash":
                self._backslash()
            elif c in " \t\r":
                self._end_word()
                self.i += 1
            elif c in "\n;":
                self._end_part(piped=False)
                self.items.append(";")
                self.i += 1
            elif c in "()":
                self._end_part(piped=False)
                self.items.append(c)
                self.i += 1
            elif c in "&|":
                self._operator(c)
            elif c in "{}":
                raise _Blocked("braces (brace expansion, groups, script blocks) aren't allowed.")
            elif c in "<>":
                self._redirect(c)
            elif c == "#" and not self.started:
                while self.i < n and text[self.i] != "\n":
                    self.i += 1
            else:
                if c in WILDCARDS:
                    self.globbed = True
                self.buf.append(c)
                self.started = True
                self.i += 1
        if self.redirect and not self.started:
            raise _Blocked("a redirect has no target.")
        self._end_part(piped=False)

    def _single_quoted(self) -> None:
        text, j = self.text, self.i + 1
        while True:
            if j >= len(text):
                raise _Blocked("a quote isn't closed.")
            if text[j] == "'":
                if self.shell == "powershell" and text[j + 1 : j + 2] == "'":
                    self.buf.append("'")
                    j += 2
                    continue
                break
            self.buf.append(text[j])
            j += 1
        self.started = self.literal = True
        self.i = j + 1

    def _double_quoted(self) -> None:
        text, j = self.text, self.i + 1
        while True:
            if j >= len(text):
                raise _Blocked("a quote isn't closed.")
            d = text[j]
            if d == '"':
                if self.shell == "powershell" and text[j + 1 : j + 2] == '"':
                    self.buf.append('"')
                    j += 2
                    continue
                break
            if d in "$`":
                raise _Blocked(_EXPANSION)
            if d == "\\" and self.shell == "bash" and text[j + 1 : j + 2] in ("\\", '"'):
                self.buf.append(text[j + 1])
                j += 2
                continue
            self.buf.append(d)
            j += 1
        self.started = self.literal = True
        self.i = j + 1

    def _dollar(self) -> None:
        rest = self.text[self.i :]
        null = re.match(r"\$null\b", rest, re.IGNORECASE)
        if self.shell == "powershell" and null and not self.started:
            self.buf.extend("$null")
            self.started = True
            self.i += len(null.group(0))
            return
        raise _Blocked(_EXPANSION)

    def _backslash(self) -> None:
        nxt = self.text[self.i + 1 : self.i + 2]
        if not nxt:
            raise _Blocked("the command ends in a backslash.")
        if nxt != "\n":  # a backslash-newline continues the line
            self.buf.append(nxt)
            self.started = self.literal = True
        self.i += 2

    def _operator(self, c: str) -> None:
        double = self.text[self.i : self.i + 2] in ("&&", "||")
        self._end_part(piped=c == "|" and not double)
        if not double and self.items and isinstance(self.items[-1], _Part):
            self.items[-1].detached = True  # `a | b` and `a &` run a in its own process
        self.items.append(self.text[self.i : self.i + 2] if double else c)
        self.i += 2 if double else 1

    def _redirect(self, c: str) -> None:
        if c == "<":
            raise _Blocked("input redirects and here-docs aren't allowed; pass files as arguments.")
        if self.started and "".join(self.buf).isdigit():
            self.buf, self.started = [], False  # a descriptor, as in 2>
        else:
            self._end_word()
        j = self.i + 1
        if self.text[j : j + 1] == ">":
            j += 1
        if self.text[j : j + 1] == "&":  # >&1, 2>&1
            k = j + 1
            while k < len(self.text) and self.text[k].isdigit():
                k += 1
            if k == j + 1:
                raise _Blocked("this redirect can't be checked; write it as 2>&1.")
            self.i = k
            return
        self.redirect = True
        self.i = j

    def _is_null(self, word: str, literal: bool) -> bool:
        """The null device for this shell and platform. `nul` is a plain file name outside
        Windows, Bash on Windows (Git Bash) writes the device as `/dev/null`, and a quoted
        `'$null'` is a file name, not the variable."""
        if self.shell == "bash":
            return word == "/dev/null"
        if word.casefold() == "$null":
            return not literal
        return word.casefold() in ("nul", "nul:") if self.windows else word == "/dev/null"

    def _end_word(self) -> None:
        if not self.started:
            return
        word = "".join(self.buf)
        # PowerShell cmdlets expand wildcards in paths themselves, even quoted ones.
        wild = self.globbed or (self.shell == "powershell" and any(c in word for c in WILDCARDS))
        literal = self.literal
        self.buf, self.started, self.globbed, self.literal = [], False, False, False
        if self.redirect:
            self.redirect = False
            if not self._is_null(word, literal):
                raise _Blocked(
                    f"the redirect to {word} writes a file; redirect only to the null device."
                )
        else:
            self.words.append(word)
            self.wild.append(wild)

    def _end_part(self, *, piped: bool) -> None:
        self._end_word()
        if self.redirect:
            raise _Blocked("a redirect has no target.")
        if self.words:
            self.items.append(_Part(self.words, self.wild, self.piped))
        self.words, self.wild = [], []
        self.piped = piped
