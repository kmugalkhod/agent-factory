"""Running one of the repo's commands (`commands.test` in `factory.yaml`) in a worktree.

The gates run the test command themselves rather than trusting an agent's word (design: gates
between stages). The command is the repo's, the tests in it were written by an agent, and the
engine holds the user's credentials, so:

- No shell. The command is split like a POSIX shell would split it (`shlex`, so `\\` escapes:
  write paths with `/` or in single quotes) and shell operators (`&&`, `|`, `;`, redirects,
  `$(...)`, backticks) are refused: put several steps in a script and name the script.
- A clean environment, built from an allowlist (the system basics in `providers.BASE_VARS`).
  Credentials (`CLAUDE_*`, `ANTHROPIC_*`, `GH_*`, `GITHUB_*`, ...) never reach the command, even
  when the allowlist names them.
- A program named without a folder is looked up on the absolute `PATH` entries only, so one
  planted in the worktree never runs. A timeout stops the whole process tree.

For pytest and vitest the engine asks for a JUnit XML report in the run folder and reads it
into test names and outcomes; other runners give the exit code only. Each run saves its output
to `<run folder>/test-output-<stage>-<attempt>.txt` and logs a `test_run` event.
"""

import contextlib
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from factory_engine.errors import ConfigError, GateError
from factory_engine.events import EventLog, TestRun
from factory_engine.git_ops import env_get, find_program, run_command
from factory_engine.providers import BASE_VARS
from factory_engine.run import Role

type Runner = Callable[
    [Sequence[str], Path, Mapping[str, str], float], subprocess.CompletedProcess[str]
]
"""Runs argv in a folder with an environment and a timeout: `git_ops.run_command`, or a fake."""

Framework = Literal["pytest", "vitest"]
Outcome = Literal["passed", "failed", "error", "skipped"]

DEFAULT_TIMEOUT = 600.0
"""Seconds a test command may run. `factory.yaml` has no setting for it yet."""
TIMED_OUT = -1
"""The exit code a `test_run` event records for a command stopped at its timeout."""

# Variable name prefixes that hold, or point at, credentials. Never passed to a repo command.
CREDENTIAL_PREFIXES = ("ANTHROPIC_", "CLAUDE_", "GH_", "GITHUB_", "LITELLM_", "AWS_")
# Set for every command: no `__pycache__` in the worktree, and Python output in UTF-8.
COMMAND_VARS: Mapping[str, str] = {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}

_OPERATOR_CHARS = frozenset("();<>|&")
_SUBSTITUTIONS = ("`", "$(", "\n", "\r")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
_PROGRAM_SUFFIXES = (".exe", ".cmd", ".bat", ".com")
_PYTHON = re.compile(r"python[0-9.]*|py|pypy[0-9.]*")
_PYTEST = re.compile(r"py\.?test(-?[0-9.]+)?")
_COLLECTION_FAILURE = "collection failure"  # pytest's message for a file it couldn't collect
_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"
# A test case's child element and the outcome it means, most serious first.
_OUTCOME_TAGS: tuple[tuple[str, Outcome], ...] = (
    ("error", "error"),
    ("failure", "failed"),
    ("skipped", "skipped"),
)


# ------------------------------------------------------------------ the command


def parse_command(command: str) -> list[str]:
    """The command as an argument list, or ConfigError for one that needs a shell."""
    if not command.strip():
        raise ConfigError(
            "the command is empty. Set it in factory.yaml `commands`, for example "
            "`test: uv run pytest`."
        )
    if any(text in command for text in _SUBSTITUTIONS):
        raise ConfigError(_needs_shell(command, "command substitution or a line break"))
    try:
        # Keeps quotes on its tokens, so a quoted `&&` is an argument, not an operator.
        lexer = shlex.shlex(command, posix=False, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
        argv = shlex.split(command, posix=True)
    except ValueError as err:
        raise ConfigError(
            f"the command `{command}` can't be split into arguments ({err}). Check its quotes."
        ) from err
    operator = next((t for t in tokens if t and set(t) <= _OPERATOR_CHARS), None)
    if operator is not None:
        raise ConfigError(_needs_shell(command, f"the shell operator `{operator}`"))
    if not argv:
        raise ConfigError(f"the command `{command}` names no program.")
    if _ASSIGNMENT.fullmatch(argv[0]):
        raise ConfigError(
            f"the command `{command}` starts with a variable assignment, which needs a shell. "
            "Set the variable inside a script and put the script in factory.yaml `commands`."
        )
    return argv


def _needs_shell(command: str, what: str) -> str:
    return (
        f"the command `{command}` uses {what}, and the engine runs commands without a shell. "
        "Put the steps in a script in the repo (for example `scripts/test.cmd` or a Python "
        "file) and set factory.yaml `commands` to run that script."
    )


type _Found = tuple[Framework | None, int]
"""A way the command could start: the runner (None for any other program) and the index just
after the runner's name in argv."""
_NOT_A_RUNNER: _Found = (None, 0)


def detect_runner(argv: Sequence[str]) -> Framework | None:
    """`pytest` or `vitest` when the command runs one of them, else None. Raises ConfigError
    when that can't be told for certain (see `locate_runner`)."""
    return locate_runner(argv)[0]


def locate_runner(argv: Sequence[str]) -> tuple[Framework | None, int]:
    """The test runner the command starts, and where the engine's report options go.

    Only the command position counts, never an argument: the program itself (`pytest`,
    `py.test`, `vitest`, with any folder or Windows extension), `<python> [options] -m
    pytest`, or the program a known launcher starts after its own options: `uv [options] run
    [options]` (also `uv run -m pytest`, `uv tool run`, `uvx`), `poetry`/`pdm`/`pipenv ...
    run`, `npx`, `npm exec`, `pnpm [exec|dlx]`, `yarn [exec|dlx]`.

    The options go after the user's own (so the engine's win: pytest and vitest take the last
    value given) and before the runner's `--`, after which they would be file names. Returns
    `(None, len(argv))` for any other program.

    Raises ConfigError when the command form can't be read for certain: a launcher option the
    engine doesn't know, if one reading of it starts pytest or vitest and another doesn't;
    `npm`/`pnpm`/`yarn run pytest|vitest` (a package script or the program, can't tell); and
    options that would stop the report (`-p no:junitxml`, vitest's own `--outputFile`).
    """
    found = _starts(argv, 0)
    if len(found) > 1 and any(runner is not None for runner, _ in found):
        raise ConfigError(_unclear(argv, "it uses a launcher option the engine doesn't know"))
    runner, after = found.pop() if len(found) == 1 else _NOT_A_RUNNER
    if runner is None:
        return None, len(argv)
    end = next((i for i in range(after, len(argv)) if argv[i] == "--"), len(argv))
    _refuse_report_blockers(argv, runner, after, end)
    return runner, end


def _unclear(argv: Sequence[str], why: str) -> str:
    return (
        f"the engine can't tell for certain how `{shlex.join(argv)}` starts its tests: {why}. "
        "Write the command in a plain form in factory.yaml `commands`, for example "
        "`uv run pytest -x`, `python -m pytest` or `npx vitest run`."
    )


def _refuse_report_blockers(argv: Sequence[str], runner: Framework, start: int, end: int) -> None:
    args = list(argv[start:end])
    if runner == "pytest":
        for i, arg in enumerate(args):
            plugin = args[i + 1] if arg == "-p" and i + 1 < len(args) else arg.removeprefix("-p")
            if arg.startswith("-p") and plugin.strip() == "no:junitxml":
                raise ConfigError(
                    f"`{shlex.join(argv)}` turns off pytest's JUnit report (`-p no:junitxml`), "
                    "which the tester and builder gates read. Remove that option."
                )
    elif any(arg.startswith(("--outputFile", "--output-file")) for arg in args):
        raise ConfigError(
            f"`{shlex.join(argv)}` sets vitest's `--outputFile`; the engine sets it to write "
            "its own JUnit report. Remove that option from factory.yaml `commands`."
        )


def _program_name(arg: str) -> str:
    """A program's name without its folder or Windows extension, in lower case."""
    name = PurePosixPath(arg.replace("\\", "/")).name.lower()
    for suffix in _PROGRAM_SUFFIXES:
        name = name.removesuffix(suffix)
    return name


def _starts(argv: Sequence[str], i: int) -> set[_Found]:
    """Every way the program at `argv[i]` could start (more than one when a launcher option
    the engine doesn't know may or may not take a value)."""
    if i >= len(argv):
        return {_NOT_A_RUNNER}
    name = _program_name(argv[i])
    if _PYTEST.fullmatch(name):
        return {("pytest", i + 1)}
    if name == "vitest":
        return {("vitest", i + 1)}
    if _PYTHON.fullmatch(name):
        return {_python_module(argv, i + 1)}
    if name in _LAUNCHERS:
        return _launcher(argv, i, name)
    return {_NOT_A_RUNNER}


def _python_module(argv: Sequence[str], start: int) -> _Found:
    """`python [options] -m pytest`. Python's own option grammar: short flags may be combined
    (`-Bu`), `-W` and `-X` take a value (attached or next), `-m` and `-c` end the options."""
    i = start
    while i < len(argv):
        arg = argv[i]
        if arg == "--check-hash-based-pycs":
            i += 2
            continue
        if not arg.startswith("-") or arg in ("-", "--") or arg in ("--help", "--version"):
            return _NOT_A_RUNNER  # a script, stdin, or nothing runs
        if arg.startswith("--"):
            i += 1
            continue
        step = 1
        for k, char in enumerate(arg[1:], start=1):
            rest = arg[k + 1 :]
            if char == "m":
                module, after = (rest, i + 1) if rest else (_at(argv, i + 1), i + 2)
                return ("pytest", after) if _PYTEST.fullmatch(module) else _NOT_A_RUNNER
            if char == "c":
                return _NOT_A_RUNNER
            if char in "WX":
                step = 1 if rest else 2
                break
        i += step
    return _NOT_A_RUNNER


def _launcher(argv: Sequence[str], i: int, name: str) -> set[_Found]:
    found: set[_Found] = set()
    if name in ("uvx", "bunx", "npx"):
        options = _UV_RUN_OPTIONS if name == "uvx" else _NODE_OPTIONS
        for k in _after_options(argv, i + 1, options):
            found |= _uv_program(argv, k) if name == "uvx" else _starts(argv, k)
        return found
    for k in _after_options(argv, i + 1, _GLOBAL_OPTIONS[name]):
        command = _at(argv, k)
        if name == "uv":
            if command == "run":
                run_options = k + 1
            elif command == "tool" and _at(argv, k + 1) == "run":
                run_options = k + 2
            else:
                found.add(_NOT_A_RUNNER)
                continue
            for j in _after_options(argv, run_options, _UV_RUN_OPTIONS):
                found |= _uv_program(argv, j)
        elif name in ("poetry", "pdm", "pipenv"):
            if command == "run":
                for j in _after_options(argv, k + 1, _GLOBAL_OPTIONS[name]):
                    found |= _starts(argv, j)
            else:
                found.add(_NOT_A_RUNNER)
        else:  # npm, pnpm, yarn
            found |= _node_command(argv, k, name)
    return found


def _uv_program(argv: Sequence[str], k: int) -> set[_Found]:
    """What `uv run` starts at `argv[k]`: a program, or a module with `-m`/`--module`."""
    arg = _at(argv, k)
    if arg in ("-m", "--module") or arg.startswith("--module="):
        module, after = (_at(argv, k + 1), k + 2) if "=" not in arg else (arg[9:], k + 1)
        return {("pytest", after) if _PYTEST.fullmatch(module) else _NOT_A_RUNNER}
    return _starts(argv, k)


def _node_command(argv: Sequence[str], k: int, name: str) -> set[_Found]:
    """`npm|pnpm|yarn <command>`: `exec`/`dlx`/`x` start a program; `pnpm`/`yarn` also run a
    program named directly (`pnpm vitest`); `run` runs a package script, or for some the
    program of that name, so `run vitest` can't be read for certain."""
    command = _at(argv, k)
    if command in ("exec", "dlx", "x"):
        found: set[_Found] = set()
        for j in _after_options(argv, k + 1, _NODE_OPTIONS):
            found |= _starts(argv, j)
        return found
    if command in ("run", "run-script"):
        script = next((a for a in argv[k + 1 :] if not a.startswith("-")), "")
        if _PYTEST.fullmatch(_program_name(script)) or _program_name(script) == "vitest":
            raise ConfigError(
                _unclear(argv, f"`{name} {command} {script}` may run a package script")
            )
        return {_NOT_A_RUNNER}
    if name != "npm" and command and not command.startswith("-"):
        program = _program_name(command)
        if _PYTEST.fullmatch(program) or program == "vitest":
            return _starts(argv, k)
    return {_NOT_A_RUNNER}


def _after_options(argv: Sequence[str], start: int, options: "_Options") -> set[int]:
    """Where a launcher's options starting at `start` may end: the index of the first argument
    that isn't one of them (after a `--`, the next one). An option the engine doesn't know
    may or may not take a value, so both readings are kept."""
    ends: set[int] = set()
    seen: set[int] = set()
    todo = [start]
    while todo:
        i = todo.pop()
        if i in seen:
            continue
        seen.add(i)
        arg = _at(argv, i)
        stop = arg in options.stops or ("--module" in options.stops and arg.startswith("--module="))
        if not arg.startswith("-") or arg == "-" or stop:
            ends.add(i)
        elif arg == "--":
            ends.add(i + 1)
        elif arg in options.values:
            todo.append(i + 2)
        elif arg in options.flags or (arg.startswith("--") and "=" in arg):
            todo.append(i + 1)
        elif not arg.startswith("--") and arg[:2] in options.values:
            todo.append(i + 1)  # a short option with its value attached: `-Fweb`
        else:
            todo += [i + 1, i + 2]  # unknown: a flag, or followed by its value
    return ends


def _at(argv: Sequence[str], i: int) -> str:
    return argv[i] if 0 <= i < len(argv) else ""


@dataclass(frozen=True)
class _Options:
    """A launcher's options: those followed by a value, flags, and those that end them."""

    values: frozenset[str]
    flags: frozenset[str] = frozenset()
    stops: frozenset[str] = frozenset()


_UV_GLOBAL_FLAGS = frozenset(
    {
        "-q",
        "--quiet",
        "-v",
        "--verbose",
        "-n",
        "--no-cache",
        "--offline",
        "--no-offline",
        "--native-tls",
        "--no-native-tls",
        "--no-progress",
        "--no-config",
        "--preview",
        "--no-preview",
        "--managed-python",
        "--no-managed-python",
        "--no-python-downloads",
        "--allow-python-downloads",
    }
)
_UV_GLOBAL_VALUES = frozenset(
    {
        "--directory",
        "--project",
        "--cache-dir",
        "--config-file",
        "--python",
        "-p",
        "--color",
        "--allow-insecure-host",
        "--python-preference",
    }
)
_UV_RUN_OPTIONS = _Options(
    values=_UV_GLOBAL_VALUES
    | {
        "--with",
        "--with-editable",
        "--with-requirements",
        "--from",
        "--extra",
        "--no-extra",
        "--group",
        "--only-group",
        "--no-group",
        "--env-file",
        "--package",
        "--index",
        "--default-index",
        "--index-url",
        "-i",
        "--extra-index-url",
        "--find-links",
        "-f",
        "--index-strategy",
        "--keyring-provider",
        "--resolution",
        "--prerelease",
        "--exclude-newer",
        "--link-mode",
        "--refresh-package",
        "--reinstall-package",
        "--upgrade-package",
        "-P",
        "--no-binary-package",
        "--no-build-package",
        "--python-platform",
    },
    flags=_UV_GLOBAL_FLAGS
    | {
        "--frozen",
        "--locked",
        "--no-sync",
        "--all-extras",
        "--all-groups",
        "--all-packages",
        "--dev",
        "--no-dev",
        "--only-dev",
        "--no-default-groups",
        "--isolated",
        "--active",
        "--no-active",
        "--no-editable",
        "--exact",
        "--inexact",
        "--no-project",
        "--no-env-file",
        "--no-install-project",
        "--no-install-workspace",
        "--no-install-local",
        "--compile-bytecode",
        "--refresh",
        "-U",
        "--upgrade",
        "--reinstall",
        "--no-build-isolation",
        "--no-build",
        "--no-binary",
        "--no-sources",
        "-s",
        "--script",
        "--gui-script",
        "--show-resolution",
    },
    stops=frozenset({"-m", "--module"}),
)
_NODE_OPTIONS = _Options(
    values=frozenset(
        {
            "-p",
            "--package",
            "-c",
            "--call",
            "--prefix",
            "-w",
            "--workspace",
            "--filter",
            "-F",
            "--dir",
            "-C",
            "--cwd",
            "--userconfig",
            "--cache",
            "--loglevel",
            "--reporter",
        }
    ),
    flags=frozenset(
        {
            "-y",
            "--yes",
            "--no",
            "-q",
            "--quiet",
            "-s",
            "--silent",
            "--no-install",
            "--ignore-existing",
            "-ws",
            "--workspaces",
            "--include-workspace-root",
            "-r",
            "--recursive",
            "--workspace-root",
            "--parallel",
            "--stream",
            "--if-present",
            "--shell-mode",
        }
    ),
)
_POETRY_OPTIONS = _Options(
    values=frozenset({"-C", "--directory", "-P", "--project"}),
    flags=frozenset(
        {
            "-q",
            "--quiet",
            "-v",
            "-vv",
            "-vvv",
            "--verbose",
            "-n",
            "--no-interaction",
            "--ansi",
            "--no-ansi",
            "--no-plugins",
            "--no-cache",
        }
    ),
)
_GLOBAL_OPTIONS: Mapping[str, _Options] = {
    "uv": _Options(values=_UV_GLOBAL_VALUES, flags=_UV_GLOBAL_FLAGS),
    "poetry": _POETRY_OPTIONS,
    "pdm": _Options(
        values=frozenset({"-p", "--project"}),
        flags=frozenset({"-q", "--quiet", "-v", "--verbose", "-g", "--global", "-l", "--list"}),
    ),
    "pipenv": _Options(values=frozenset({"--python"}), flags=frozenset({"-q", "--quiet"})),
    "npm": _NODE_OPTIONS,
    "pnpm": _NODE_OPTIONS,
    "yarn": _NODE_OPTIONS,
}
_LAUNCHERS = frozenset({"uvx", "bunx", "npx", *_GLOBAL_OPTIONS})


def report_args(runner: Framework | None, report: Path, cache: Path) -> list[str]:
    """Arguments added to the command so the runner writes a JUnit report to `report`. pytest
    writes the `xunit1` form, whose test cases name their file, and keeps its cache in `cache`
    (the run folder), out of the worktree. Both paths must be absolute: the command runs in
    the worktree."""
    if runner == "pytest":
        return [
            f"--junitxml={report}",
            "-o",
            "junit_family=xunit1",
            "-o",
            f"cache_dir={cache}",
        ]
    if runner == "vitest":
        return ["--reporter=default", "--reporter=junit", f"--outputFile.junit={report}"]
    return []


def with_report_args(
    argv: Sequence[str], report: Path, cache: Path
) -> tuple[list[str], Framework | None]:
    """`argv` with `report_args` placed where `locate_runner` says (after the user's own
    options, before the runner's `--`), and the runner. Unchanged for other runners."""
    runner, at = locate_runner(argv)
    full = list(argv)
    full[at:at] = report_args(runner, report, cache)
    return full, runner


# ------------------------------------------------------------------ the environment


def is_credential(name: str) -> bool:
    return name.upper().startswith(CREDENTIAL_PREFIXES)


def command_env(
    parent: Mapping[str, str],
    allow: Iterable[str] = BASE_VARS,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The environment for a repo command: the variables in `allow` taken from `parent` (the
    engine's environment; pass `os.environ`), then `COMMAND_VARS`, then `extra`. Credentials
    are dropped from `allow` and refused in `extra`; nothing else is copied from `parent`."""
    by_upper = {key.upper(): value for key, value in parent.items()}
    env = {
        name: by_upper[name.upper()]
        for name in allow
        if name.upper() in by_upper and not is_credential(name)
    }
    env.update(COMMAND_VARS)
    for name, value in (extra or {}).items():
        if is_credential(name):
            raise ConfigError(
                f"{name} looks like a credential, and credentials never reach repo commands. "
                "Give the tests a test-only value under another name."
            )
        env[name] = value
    return env


def resolve_program(name: str, worktree: Path, env: Mapping[str, str]) -> Path:
    """The program to start. A name with a folder is taken as given (relative to the
    worktree); a bare name is looked up on the absolute `PATH` entries only."""
    if "/" in name or "\\" in name:
        path = Path(name)
        return path if path.is_absolute() else worktree / path
    found = _which(name, env)
    if found is None:
        raise ConfigError(
            f"`{name}` isn't on PATH, so the command can't run. Install it, or give its full "
            "path in factory.yaml `commands`."
        )
    return found


def _which(name: str, env: Mapping[str, str]) -> Path | None:
    """Like `git_ops.find_program`, but also finds a Windows name given with its extension."""
    if sys.platform != "win32":
        return find_program(name, env)
    pathext = env_get(env, "PATHEXT") or _DEFAULT_PATHEXT
    if Path(name).suffix.lower() not in pathext.lower().split(";"):
        return find_program(name, env)
    for entry in env_get(env, "PATH").split(os.pathsep):
        folder = Path(entry.strip().strip('"'))
        if entry.strip() and folder.is_absolute() and (folder / name).is_file():
            return folder / name
    return None


# ------------------------------------------------------------------ JUnit reports


@dataclass(frozen=True)
class JUnitCase:
    classname: str
    name: str
    outcome: Outcome
    message: str = ""
    file: str = ""  # the test's file relative to the runner's root, when the report gives it

    @property
    def id(self) -> str:
        """`classname::name`, or the name alone when there is no class (a collection error)."""
        return f"{self.classname}::{self.name}" if self.classname else self.name

    @property
    def collection_error(self) -> bool:
        return self.outcome == "error" and _COLLECTION_FAILURE in self.message.lower()


@dataclass(frozen=True)
class JUnitReport:
    cases: tuple[JUnitCase, ...]
    suite_errors: int = 0  # the errors the test suites count, some of which no case carries

    def _count(self, outcome: Outcome) -> int:
        return sum(1 for case in self.cases if case.outcome == outcome)

    @property
    def passed(self) -> int:
        return self._count("passed")

    @property
    def failed(self) -> int:
        return self._count("failed")

    @property
    def skipped(self) -> int:
        return self._count("skipped")

    @property
    def errors(self) -> int:
        """Errors outside a test's own assertions: collection, syntax, fixtures, setup."""
        return max(self._count("error"), self.suite_errors)

    @property
    def collection_errors(self) -> tuple[str, ...]:
        return tuple(case.id for case in self.cases if case.collection_error)


def read_junit(path: Path) -> JUnitReport:
    """Read a JUnit XML report (pytest's or vitest's). Raises ValueError naming the file when
    it is missing or isn't a report."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as err:
        raise ValueError(f"{path}: can't read the JUnit report ({err}).") from err
    if root.tag not in ("testsuites", "testsuite"):
        raise ValueError(f"{path}: not a JUnit report (its root is <{root.tag}>).")
    cases = tuple(_case(element) for element in root.iter("testcase"))
    suites = [root] if root.tag == "testsuite" else []
    suites += list(root.iter("testsuite")) if root.tag == "testsuites" else []
    suite_errors = sum(_int(suite.get("errors")) for suite in suites)
    return JUnitReport(cases=cases, suite_errors=suite_errors)


def _case(element: ET.Element) -> JUnitCase:
    classname, name = element.get("classname", ""), element.get("name", "")
    file = element.get("file", "")
    for tag, outcome in _OUTCOME_TAGS:
        found = element.find(tag)
        if found is not None:
            message = found.get("message") or (found.text or "").strip()
            first = message.splitlines()[0] if message else ""
            return JUnitCase(classname, name, outcome, first, file)
    return JUnitCase(classname, name, "passed", file=file)


def _int(text: str | None) -> int:
    try:
        return max(0, int(text or "0"))
    except ValueError:
        return 0


# ------------------------------------------------------------------ running it


@dataclass(frozen=True)
class CommandResult:
    """One run of a repo command. `exit_code` is None when it was stopped at its timeout;
    `report` is None for runners other than pytest and vitest, or when no usable report was
    written (`report_problem` says why)."""

    command: str
    argv: tuple[str, ...]
    runner: Framework | None
    exit_code: int | None
    timed_out: bool
    timeout: float
    seconds: float
    output: str
    output_path: Path
    report: JUnitReport | None
    report_problem: str | None


def run_repo_command(
    command: str,
    *,
    worktree: Path,
    run_folder: Path,
    stage: Role,
    attempt: int,
    parent_env: Mapping[str, str],
    events: EventLog,
    timeout: float = DEFAULT_TIMEOUT,
    allow: Iterable[str] = BASE_VARS,
    extra: Mapping[str, str] | None = None,
    runner: Runner = run_command,
    timer: Callable[[], float] = time.monotonic,
) -> CommandResult:
    """Run `command` in `worktree` with a clean environment built from `parent_env`, save its
    output to the run folder and log a `test_run` event for `stage`.

    Raises ConfigError, before anything runs, for a command that needs a shell or a program
    that can't be found or started.
    """
    if attempt < 1:
        raise ValueError(f"attempt {attempt} is invalid; attempts count from 1.")
    # The command runs in the worktree, so every path handed to it must be absolute.
    worktree, run_folder = worktree.resolve(), run_folder.resolve()
    argv = parse_command(command)
    env = command_env(parent_env, allow, extra)
    program = resolve_program(argv[0], worktree, env)
    report_path = run_folder / f"junit-{stage}-{attempt}.xml"
    output_path = run_folder / f"test-output-{stage}-{attempt}.txt"
    full, framework = with_report_args([str(program), *argv[1:]], report_path, run_folder / "cache")
    try:  # a report left from an earlier run must never stand in for this one
        report_path.unlink(missing_ok=True)
    except OSError as err:
        raise GateError(f"{report_path}: can't remove the old report ({err}).") from err

    started = timer()
    try:
        done = runner(full, worktree, env, timeout)
    except subprocess.TimeoutExpired:
        done = None
    except (OSError, ValueError) as err:
        raise ConfigError(
            f"the command `{command}` couldn't start in {worktree} ({err}). Check factory.yaml "
            "`commands` and that its program is installed."
        ) from err
    seconds = max(0.0, timer() - started)

    report: JUnitReport | None = None
    problem: str | None = None
    if done is None:
        output = (
            f"timeout: the command ran longer than {timeout:g}s and was stopped, with every "
            "process it started. Its output is lost.\n"
        )
    else:
        output = done.stdout + done.stderr
        if framework is not None:
            try:
                report = read_junit(report_path)
            except ValueError as err:
                problem = str(err)
    shown = shlex.join(full)
    status = "timeout" if done is None else f"exit {done.returncode}"
    write_atomic(output_path, f"$ {shown}\n{output}\n[{status} after {seconds:.1f}s]\n")

    events.append(
        TestRun(
            command=command,
            exit_code=TIMED_OUT if done is None else done.returncode,
            passed=None if report is None else report.passed,
            failed=None if report is None else report.failed + report.errors,
            seconds=seconds,
        ),
        role=stage,
    )
    return CommandResult(
        command=command,
        argv=tuple(full),
        runner=framework,
        exit_code=None if done is None else done.returncode,
        timed_out=done is None,
        timeout=timeout,
        seconds=seconds,
        output=output,
        output_path=output_path,
        report=report,
        report_problem=problem,
    )


def write_atomic(path: Path, text: str) -> None:
    """Write `path` as UTF-8 through a temp file in the same folder and a replace, so a crash
    leaves the old file or the new one, never half of one."""
    temp: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
        temp = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(path)
    except OSError as err:
        if temp is not None:
            with contextlib.suppress(OSError):
                temp.unlink(missing_ok=True)
        raise GateError(f"{path}: couldn't write ({err}). Check the run folder.") from err
