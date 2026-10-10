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
        "rg": ("--pre", "--pre-glob", "-z", "--search-zip"),
        "find": ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0")
        + ("-fprintf", "-fls"),
        "tree": ("-o",),
        "stat": (),
        "file": (),
        "du": (),
        "diff": (),
        "sort": ("-o", "--output"),
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
PATTERN_FLAGS = frozenset({"-e", "--regexp", "-f", "--file", "-pattern"})
NULL_TARGETS = frozenset({"/dev/null", "nul", "nul:", "$null"})
SUFFIXES = (".exe", ".cmd", ".bat", ".com", ".ps1")

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
    allowlist: tuple[tuple[str, ...], ...]
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
        commands: list[tuple[str, ...]] = []
        for name in role_settings.shell_allowlist:
            command: str | None = getattr(settings.commands, name)
            if command:  # a command that isn't configured allows nothing
                commands.append(tuple(_lex(command, "bash")[0][0]))
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


def _check(policy: ShellPolicy, command: str, shell: Shell, cwds: set[Path]) -> None:
    for words, piped in _lex(command, shell):
        _check_part(policy, words, piped, shell, cwds)


def _check_part(
    policy: ShellPolicy, words: list[str], piped: bool, shell: Shell, cwds: set[Path]
) -> None:
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
        _cd(policy, words, args, shell, cwds)
        return
    if name in DELETE:
        _check_delete(policy, words, args, shell, cwds)
    if _allowlisted(policy, words):
        return
    if policy.read_only and name in READ_ONLY:
        _check_read_only(policy, name, words, args, piped, shell, cwds)
        return
    raise _Blocked(f"`{words[0]}` isn't allowed for the {policy.role}. {_may_run(policy)}")


def _may_run(policy: ShellPolicy) -> str:
    runs = [f"`{' '.join(c)}`" for c in policy.allowlist]
    if policy.read_only:
        runs.append("read-only commands (ls, cat, head, grep, find, ...)")
    return f"It may run only: {', '.join(runs)}."


def _allowlisted(policy: ShellPolicy, words: list[str]) -> bool:
    for allowed in policy.allowlist:
        if (
            len(words) >= len(allowed)
            and _name(words[0]) == _name(allowed[0])
            and words[1 : len(allowed)] == list(allowed[1:])
        ):
            return True
    return False


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
    policy: ShellPolicy, words: list[str], args: list[str], shell: Shell, cwds: set[Path]
) -> None:
    targets = [a for a in args if not a.startswith("-")]
    if not targets:
        raise _Blocked(
            f"`{' '.join(words)}` goes to the home folder; cd only to folders inside the "
            f"worktree ({policy.worktree})."
        )
    moved: set[Path] = set()
    for cwd in cwds:  # every folder the shell could be in at this point
        path = _resolve(policy, targets[0], cwd, shell)
        if path is None or _relative(policy, path, policy.worktree) is None:
            raise _Blocked(
                f"`{' '.join(words)}` leaves the worktree ({policy.worktree}); cd only to "
                "folders inside it."
            )
        moved.add(path)
    cwds |= moved  # a subshell's cd may not stick, so keep the old folders too


def _check_delete(
    policy: ShellPolicy, words: list[str], args: list[str], shell: Shell, cwds: set[Path]
) -> None:
    for target in (a for a in args if not a.startswith("-")):
        for cwd in cwds:
            path = _resolve(policy, target, cwd, shell)
            rel = None if path is None else _relative(policy, path, policy.worktree)
            if not rel:  # outside, the worktree itself, or a parent of it
                raise _Blocked(
                    f"`{' '.join(words)}` deletes the worktree, a parent folder or something "
                    "outside the worktree."
                )


# --------------------------------------------------------------- read-only


def _check_read_only(
    policy: ShellPolicy,
    name: str,
    words: list[str],
    args: list[str],
    piped: bool,
    shell: Shell,
    cwds: set[Path],
) -> None:
    for arg in args:
        flag = arg.casefold()
        if any(flag == f or flag.startswith(f + "=") for f in READ_ONLY[name]):
            raise _Blocked(
                f"`{words[0]} {arg}` can change files or run commands; the {policy.role} may run "
                "read-only commands only."
            )
    paths = [a for a in args if not a.startswith("-")]
    if name in SEARCHES and paths and not any(a.casefold() in PATTERN_FLAGS for a in args):
        paths = paths[1:]  # the first one is the pattern
    for cwd in cwds:
        targets = [_resolve(policy, p, cwd, shell) for p in paths]
        if name in SEARCHES and not paths and not piped:
            targets = [cwd]  # searches the current folder
        for raw, path in zip(paths or ["."], targets, strict=False):
            if path is None or (
                _relative(policy, path, policy.worktree) is None
                and _relative(policy, path, policy.run_folder) is None
            ):
                raise _Blocked(
                    f"`{words[0]}` reads {raw}, outside the worktree and the run folder."
                )
            if name in READS_CONTENT:
                secret = _secret_under(path, policy.windows)
                if secret is not None:
                    raise _Blocked(
                        f"`{words[0]}` would read {secret}, a secrets file (.env*). Narrow the "
                        "paths so it skips the file."
                    )


def _secret_under(path: Path, windows: bool) -> Path | None:
    """The first `.env*` file at or under `path`; links aren't followed."""
    if _is_secret(path.name, windows):
        return path
    if not path.is_dir():
        return None
    folders = [path]
    while folders:
        folder = folders.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError as err:
            raise _Blocked(f"can't list {folder} to check for secrets ({err}).") from err
        for entry in entries:
            if entry.is_symlink() or entry.is_junction():
                continue
            if entry.is_dir(follow_symlinks=False):
                folders.append(Path(entry.path))
            elif _is_secret(entry.name, windows):
                return Path(entry.path)
    return None


def _is_secret(name: str, windows: bool) -> bool:
    return (name.casefold() if windows else name).startswith(".env")


# ----------------------------------------------------------------- paths


def _resolve(policy: ShellPolicy, raw: str, cwd: Path, shell: Shell) -> Path | None:
    """Where a path argument really leads, or None when it can't be judged."""
    text = raw
    if text.startswith("~") or text.replace("\\", "/").startswith("//"):
        return None  # home folder, network or device path
    if policy.windows and shell == "bash":
        drive = re.match(r"/([A-Za-z])(/|$)", text)
        if drive:  # Git Bash writes C:\x as /c/x
            text = f"{drive.group(1)}:/{text[drive.end() :]}"
    path = Path(text)
    if not path.is_absolute() and (path.drive or path.root):
        return None  # drive-relative (`C:x`) or rooted without a drive (`\x`, `/x` on Windows)
    try:
        return (path if path.is_absolute() else cwd / path).resolve()
    except (OSError, ValueError, RuntimeError):
        return None


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
    """A command word as a bare, lower-case program name: `C:\\Git\\bin\\Git.EXE` is `git`."""
    base = re.split(r"[\\/]", word)[-1].casefold()
    for suffix in SUFFIXES:
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


# ----------------------------------------------------------------- lexer


def _lex(command: str, shell: Shell) -> list[tuple[list[str], bool]]:
    """Split a command into parts of words, quotes removed. Each part says whether a pipe
    feeds it. Raises `_Blocked` for anything the check can't judge."""
    if re.search(r"%[A-Za-z_][\w()]*%", command):
        raise _Blocked(_EXPANSION)
    lexer = _Lexer(command, shell)
    lexer.run()
    return lexer.parts


class _Lexer:
    def __init__(self, command: str, shell: Shell) -> None:
        self.text = command
        self.shell = shell
        self.parts: list[tuple[list[str], bool]] = []
        self.words: list[str] = []
        self.buf: list[str] = []
        self.started = False  # a word has begun, even an empty quoted one
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
            elif c in "\n;()":
                self._end_part(piped=False)
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
        self.started = True
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
        self.started = True
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
            self.started = True
        self.i += 2

    def _operator(self, c: str) -> None:
        double = self.text[self.i : self.i + 2] in ("&&", "||")
        self._end_part(piped=c == "|" and not double)
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

    def _end_word(self) -> None:
        if not self.started:
            return
        word = "".join(self.buf)
        self.buf, self.started = [], False
        if self.redirect:
            self.redirect = False
            if word.casefold() not in NULL_TARGETS:
                raise _Blocked(
                    f"the redirect to {word} writes a file; redirect only to the null device."
                )
        else:
            self.words.append(word)

    def _end_part(self, *, piped: bool) -> None:
        self._end_word()
        if self.redirect:
            raise _Blocked("a redirect has no target.")
        if self.words:
            self.parts.append((self.words, self.piped))
        self.words = []
        self.piped = piped
