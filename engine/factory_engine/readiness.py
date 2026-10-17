"""The readiness check (design: "Bootstrap, onboarding and readiness"; task 2.25).

No feature run starts on a repo until it meets the readiness bar, because tests are how the
factory checks its own work. Before a feature or spec run the engine checks, and reports every
condition that fails, each with what to add or fix:

1. Isolation: a repo with `sandbox_required` is refused on native Windows, where agents run in
   a worktree, which doesn't confine them. The repo's commands aren't run then.
2. GitHub: `origin` exists and points at GitHub; `git ls-remote origin` reaches it without
   asking for a login (the engine's `Git` never prompts, and a short timeout catches a hidden
   login window); origin has a `main` branch; `gh auth status` succeeds.
3. On origin/main, which every run's worktree starts from (a file only in your checkout doesn't
   count): `CLAUDE.md` and `factory.yaml` exist at the repo root, and each critical flow in
   `factory.yaml` names at least one test that exists (a file or folder, relative to the repo
   root, optionally followed by `::` and a test's name).
4. The build, test, lint and smoke commands (factory.yaml `commands`) are each set and pass in
   a temporary checkout of origin/main, run like the gates run the test command (`testrun`: no
   shell, a clean environment, a timeout that stops the whole process tree). The checkout is
   removed afterwards.

When origin/main can't be read, steps 3 and 4 are reported as not checked (commands that are
missing or can't be parsed are still named).
"""

import contextlib
import dataclasses
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlsplit

from factory_engine.config import CommandName, CriticalFlow, Settings
from factory_engine.errors import ConfigError, GitError, GitHubError, NotReadyError
from factory_engine.git_ops import Git, run_command
from factory_engine.github import GitHub
from factory_engine.safety.secrets import redact
from factory_engine.testrun import (
    DEFAULT_TIMEOUT,
    Runner,
    command_env,
    locate_runner,
    parse_command,
    resolve_program,
)

REMOTE_TIMEOUT = 60.0
"""Seconds `git ls-remote` and `gh auth status` may take. A login window nobody sees would
otherwise hold the check (and later a run's push) until git's own timeout."""
TAIL_LINES = 20
"""Lines of a failing command's output shown in its message."""
CLAUDE_MD = "CLAUDE.md"
FACTORY_YAML = "factory.yaml"
COMMANDS: tuple[CommandName, ...] = ("build", "test", "lint", "smoke")
GITHUB_HOSTS = frozenset({"github.com", "ssh.github.com"})
_URL_SCHEMES = frozenset({"https", "http", "ssh", "git", "git+ssh", "ssh+git"})
# `[user@]host:path`, git's short form for ssh. A host has a dot, so `D:/repos/x` isn't one.
_SCP_LIKE = re.compile(r"^(?:[^@/\s]+@)?(?P<host>[^:/\s]*\.[^:/\s]*):(?P<path>[^/].*)$")
_USERINFO = re.compile(r"(?<=://)[^/@\s]*@")
_DRIVE = re.compile(r"^[A-Za-z]:")

Condition = Literal[
    "sandbox",
    "origin",
    "origin_reachable",
    "origin_main",
    "gh_auth",
    "unchecked",
    "claude_md",
    "factory_yaml",
    "critical_flow",
    "build",
    "test",
    "lint",
    "smoke",
]

_DOES: Mapping[CommandName, str] = {
    "build": "builds the project",
    "test": "runs the tests",
    "lint": "lints the code",
    "smoke": "runs the smoke tests (the app starts, or a health endpoint returns 200)",
}
_EXAMPLES: Mapping[CommandName, str] = {
    "build": "`uv build`, `npm run build`, or for Python code with no build step "
    "`python -m compileall -q src`",
    "test": "`uv run pytest` or `python -m pytest`",
    "lint": "`uv run ruff check .`, `ruff check .` or `npm run lint`",
    "smoke": "`python -m pytest tests/smoke` or `npx playwright test smoke`",
}


@dataclass(frozen=True)
class Problem:
    """One failing condition and what to add or fix."""

    condition: Condition
    message: str


@dataclass(frozen=True)
class Readiness:
    """The check's result: every failing condition, in the order the module docstring gives."""

    problems: tuple[Problem, ...] = ()

    @property
    def ready(self) -> bool:
        return not self.problems

    def require(self, repo: Path) -> None:
        """Raise NotReadyError listing every problem, numbered, unless the repo is ready."""
        if self.ready:
            return
        items = []
        for number, problem in enumerate(self.problems, start=1):
            first, *rest = problem.message.splitlines() or [""]
            items.append("\n".join([f"{number}. {first}", *(f"   {line}" for line in rest)]))
        raise NotReadyError(
            f"{repo} isn't ready for a feature run, so no run was started. Fix each of these, "
            "then start again:\n\n" + "\n\n".join(items)
        )


class ReadinessCheck(Protocol):
    """`check_readiness`, or a stand-in for tests."""

    def __call__(
        self,
        repo: Path,
        settings: Settings,
        *,
        git: Git,
        github: GitHub,
        parent_env: Mapping[str, str],
        platform: str,
        scratch: Path,
    ) -> Readiness: ...


def check_readiness(
    repo: Path,
    settings: Settings,
    *,
    git: Git,
    github: GitHub,
    parent_env: Mapping[str, str],
    platform: str,
    scratch: Path,
    remote: str = "origin",
    base: str = "main",
    command_timeout: float = DEFAULT_TIMEOUT,
    remote_timeout: float = REMOTE_TIMEOUT,
    runner: Runner = run_command,
) -> Readiness:
    """Check the readiness bar for `repo` (its main checkout) with its merged `settings`.

    `parent_env` is the engine's environment (pass `os.environ`): git and gh run with it, the
    repo's commands get only the clean part `testrun.command_env` keeps. `platform` is
    `sys.platform`. The temporary checkout goes in a new folder under `scratch`, outside the
    repo. Raises GitError only when git itself can't run.
    """
    problems: list[Problem] = []
    refused = settings.sandbox_required and platform == "win32"
    if refused:
        problems.append(Problem("sandbox", _SANDBOX))
    ref = _remote(git, repo, remote, base, remote_timeout, problems)
    problems += _gh_auth(github, repo, remote_timeout)
    planned, command_problems = _plan_commands(settings)
    if ref is None:
        problems.append(Problem("unchecked", _unchecked(remote, base)))
        problems += [command_problems[name] for name in COMMANDS if name in command_problems]
        return Readiness(tuple(problems))
    files = _tree_files(git, repo, ref)
    required: tuple[tuple[str, Condition], ...] = (
        (CLAUDE_MD, "claude_md"),
        (FACTORY_YAML, "factory_yaml"),
    )
    for name, condition in required:
        if name not in files:
            problems.append(Problem(condition, _missing_file(name, remote, base)))
    problems += flow_problems(settings.critical_flows, files)
    if planned and not refused:
        try:
            with _checkout(git, repo, ref, scratch) as tree:
                for name, command, argv in planned:
                    problem = _run(name, command, argv, tree, parent_env, command_timeout, runner)
                    if problem is not None:
                        command_problems[name] = problem
        except GitError as err:
            problems.append(
                Problem(
                    "unchecked",
                    f"the commands couldn't be run: {remote}/{base} couldn't be checked out "
                    f"in {scratch} ({err}). Check that folder is writable, then start again.",
                )
            )
    problems += [command_problems[name] for name in COMMANDS if name in command_problems]
    return Readiness(tuple(problems))


# ------------------------------------------------------------------ isolation

_SANDBOX = (
    "factory.yaml sets `sandbox_required: true`, and on native Windows agents run in a git "
    "worktree, which separates their files but doesn't confine what they run, so the factory "
    "refuses this repo here. The alternatives: run the factory for this repo from WSL, where "
    "agents run in the Claude Code sandbox (supported after version 1), or, if worktree "
    "isolation with the engine's safety controls is enough for this repo, set "
    "`sandbox_required: false` in factory.yaml. The repo's commands weren't run."
)


# ------------------------------------------------------------------ GitHub


def github_remote(url: str) -> bool:
    """Whether a remote URL names a repository on GitHub (`github.com`, or `ssh.github.com`
    for ssh over port 443): `https://github.com/<owner>/<repo>[.git]`, `ssh://`, `git://` or
    `git@github.com:<owner>/<repo>.git`."""
    url = url.strip()
    if "://" in url:
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
        except ValueError:
            return False
        if parts.scheme.lower() not in _URL_SCHEMES:
            return False
        path = parts.path
    else:
        match = _SCP_LIKE.match(url)
        if match is None:
            return False
        host, path = match["host"].lower(), match["path"]
    segments = [part for part in path.split("/") if part]
    return host in GITHUB_HOSTS and len(segments) == 2


def _shown(text: str) -> str:
    """Text for a message: no password from a URL, no secret git or gh printed."""
    return redact(_USERINFO.sub("", text))


def _detail(done: subprocess.CompletedProcess[str]) -> str:
    text = (done.stderr.strip() or done.stdout.strip())[-1000:]
    return _shown(" ".join(text.split())) or "no output"


def _remote(
    git: Git, repo: Path, remote: str, base: str, timeout: float, problems: list[Problem]
) -> str | None:
    """Check `remote` and fetch its `base`. Returns the fetched ref, or None when it can't be
    read; each problem found goes in `problems`."""
    found = git.run(["config", "--get", f"remote.{remote}.url"], repo, check=False)
    url = found.stdout.strip() if found.returncode == 0 else ""
    if not url:
        problems.append(
            Problem(
                "origin",
                f"the repo has no `{remote}` remote, so the factory can't push a run's branch or "
                f"open its pull request. Add the GitHub repo: `git remote add {remote} "
                "https://github.com/<owner>/<repo>.git`.",
            )
        )
        return None
    shown = _shown(url)
    if not github_remote(url):
        problems.append(
            Problem(
                "origin",
                f"`{remote}` points at {shown}, not a repository on GitHub (github.com). The "
                "factory opens and merges pull requests with `gh`, so point it at the GitHub "
                f"repo: `git remote set-url {remote} https://github.com/<owner>/<repo>.git`.",
            )
        )
    login = (
        "Run `gh auth setup-git` so git signs in with the GitHub CLI's login instead of a Git "
        f"Credential Manager window, then check that `git ls-remote {remote}` works in a "
        "terminal without asking for anything."
    )
    quick = dataclasses.replace(git, timeout=timeout)
    try:
        done = quick.run(["ls-remote", remote, f"refs/heads/{base}"], repo, check=False)
    except GitError as err:
        problems.append(
            Problem(
                "origin_reachable",
                f"`git ls-remote {remote}` didn't answer within {timeout:g}s ({_shown(str(err))}). "
                f"It may be waiting for a login window you can't see. {login}",
            )
        )
        return None
    if done.returncode != 0:
        problems.append(
            Problem(
                "origin_reachable",
                f"`git ls-remote {remote}` couldn't reach {shown} without asking for a login "
                f"(exit {done.returncode}: {_detail(done)}). {login}",
            )
        )
        return None
    if not any(line.endswith(f"\trefs/heads/{base}") for line in done.stdout.splitlines()):
        problems.append(
            Problem(
                "origin_main",
                f"{remote} has no `{base}` branch. Runs start from {remote}/{base} and merge into "
                f"it, so push it: `git push -u {remote} {base}` (rename a `master` branch first "
                f"with `git branch -m master {base}`).",
            )
        )
        return None
    ref = f"refs/remotes/{remote}/{base}"
    try:
        git.run(["fetch", "--no-tags", remote, f"+refs/heads/{base}:{ref}"], repo)
    except GitError as err:
        problems.append(
            Problem(
                "origin_reachable",
                f"`git fetch {remote} {base}` failed ({_shown(str(err))}). {login}",
            )
        )
        return None
    return ref


def _gh_auth(github: GitHub, repo: Path, timeout: float) -> list[Problem]:
    quick = dataclasses.replace(github, timeout=timeout)
    try:
        quick.run(["auth", "status", "--hostname", "github.com"], repo)
    except GitHubError as err:
        return [
            Problem(
                "gh_auth",
                f"the GitHub CLI can't use a login for github.com ({_shown(str(err))}). The "
                "factory opens and merges pull requests with `gh`: log in with `gh auth login`, "
                "then run `gh auth setup-git` so git pushes with the same login.",
            )
        ]
    return []


def _unchecked(remote: str, base: str) -> str:
    return (
        f"`CLAUDE.md`, `factory.yaml`, the critical flows' tests and the commands weren't "
        f"checked, because {remote}/{base} couldn't be read. Fix the remote first, then start "
        "again to see whether anything else is missing."
    )


# ------------------------------------------------------------------ files on main


def _tree_files(git: Git, repo: Path, ref: str) -> frozenset[str]:
    out = git.run(["ls-tree", "-r", "--name-only", "-z", ref], repo).stdout
    return frozenset(name for name in out.split("\0") if name)


def _missing_file(name: str, remote: str, base: str) -> str:
    what = (
        "that says how to build, test and lint the project, its architecture, its conventions "
        "and what agents get wrong"
        if name == CLAUDE_MD
        else "with the repo's `commands` (build, test, lint and smoke) and its critical flows"
    )
    return (
        f"`{name}` isn't on {remote}/{base}. Add a `{name}` at the repo root {what}, then commit "
        f"it and push it to {base}. Runs start from {remote}/{base}, so a file only in your "
        "checkout doesn't count."
    )


def flow_problems(flows: Sequence[CriticalFlow], files: Iterable[str]) -> list[Problem]:
    """A problem for each critical flow that names no test existing in `files` (the paths in
    origin/main, relative to the repo root, with `/`). A test is a file or folder, optionally
    followed by `::` and a test's name; paths are case-sensitive, as in git."""
    paths = frozenset(files)
    folders = frozenset(
        "/".join(parts[:end])
        for path in paths
        for parts in [path.split("/")]
        for end in range(1, len(parts))
    )
    problems: list[Problem] = []
    for flow in flows:
        if not flow.tests:
            problems.append(
                Problem(
                    "critical_flow",
                    f"critical flow `{flow.name}` in factory.yaml lists no tests. Add at least "
                    "one test for it under its `tests:` (for example `tests/test_<flow>.py`), "
                    "commit it and push it to main.",
                )
            )
            continue
        found = (_test_path(entry) for entry in flow.tests)
        if any(path is not None and (path in paths or path in folders) for path in found):
            continue
        listed = ", ".join(f"`{entry}`" for entry in flow.tests)
        problems.append(
            Problem(
                "critical_flow",
                f"none of the tests listed for critical flow `{flow.name}` ({listed}) is on "
                "origin/main. Write a test for the flow and push it to main, or fix the paths "
                "in factory.yaml: relative to the repo root, with `/`.",
            )
        )
    return problems


def _test_path(entry: str) -> str | None:
    """The file or folder part of a critical flow's test, normalized, or None for a path that
    is absolute or leaves the repo."""
    path = entry.split("::", 1)[0].strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    if not path or path.startswith("/") or _DRIVE.match(path):
        return None
    parts = path.rstrip("/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    return "/".join(parts)


# ------------------------------------------------------------------ commands

type _Planned = tuple[CommandName, str, list[str]]


def _plan_commands(settings: Settings) -> tuple[list[_Planned], dict[CommandName, Problem]]:
    """The commands to run, and a problem for each one that is missing or can't run."""
    planned: list[_Planned] = []
    problems: dict[CommandName, Problem] = {}
    for name in COMMANDS:
        command = getattr(settings.commands, name)
        if command is None or not command.strip():
            problems[name] = Problem(
                name,
                f"factory.yaml sets no `commands.{name}`. The readiness bar needs one command "
                f"that {_DOES[name]}, passing on main. Add it under `commands:` in factory.yaml, "
                f"for example {_EXAMPLES[name]}, then commit and push it to main.",
            )
            continue
        try:
            argv = parse_command(command)
            if name == "test":
                locate_runner(argv)  # the test gates must be able to read how it runs
        except ConfigError as err:
            problems[name] = Problem(name, f"`commands.{name}` can't be run: {err}")
            continue
        planned.append((name, command, argv))
    return planned, problems


@contextlib.contextmanager
def _checkout(git: Git, repo: Path, ref: str, scratch: Path) -> Iterator[Path]:
    """A temporary detached worktree of `ref` in a new folder under `scratch`, removed after."""
    try:
        scratch.mkdir(parents=True, exist_ok=True)
        holder = Path(tempfile.mkdtemp(prefix="check-", dir=scratch))
    except OSError as err:
        raise GitError(f"{scratch}: can't create a folder for the checkout ({err}).") from err
    tree = holder / "tree"
    try:
        git.run(["worktree", "add", "--detach", str(tree), ref], repo)
        yield tree
    finally:
        _remove(git, repo, tree, holder)


def _remove(git: Git, repo: Path, tree: Path, holder: Path) -> None:
    """Remove the checkout and its folder; if git can't, delete the files and prune git's
    record of the checkout."""
    removed = not tree.exists()
    if not removed:
        with contextlib.suppress(GitError):
            done = git.run(["worktree", "remove", "--force", str(tree)], repo, check=False)
            removed = done.returncode == 0
    shutil.rmtree(holder, onexc=_writable_and_retry)
    if not removed:
        with contextlib.suppress(GitError):
            git.run(["worktree", "prune"], repo, check=False)


def _writable_and_retry(func: Callable[..., object], path: str, _err: BaseException) -> None:
    """For `shutil.rmtree`: clear a read-only flag (Windows) and retry once; give up quietly."""
    with contextlib.suppress(OSError):
        Path(path).chmod(stat.S_IWRITE)
        func(path)


def _run(
    name: CommandName,
    command: str,
    argv: Sequence[str],
    tree: Path,
    parent_env: Mapping[str, str],
    timeout: float,
    runner: Runner,
) -> Problem | None:
    env = command_env(parent_env)
    try:
        program = resolve_program(argv[0], tree, env)
        done = runner([str(program), *argv[1:]], tree, env, timeout)
    except ConfigError as err:
        return Problem(name, f"`commands.{name}` can't be run: {err}")
    except subprocess.TimeoutExpired:
        return Problem(
            name,
            f"`commands.{name}` (`{command}`) ran longer than {timeout:g}s on origin/main and "
            "was stopped, with every process it started. Make it finish sooner, then start "
            "again.",
        )
    except (OSError, ValueError) as err:
        return Problem(
            name,
            f"`commands.{name}` (`{command}`) couldn't start ({err}). Check the command in "
            "factory.yaml and that its program is installed.",
        )
    if done.returncode == 0:
        return None
    output = (done.stdout + done.stderr).strip().splitlines()[-TAIL_LINES:]
    tail = "\n".join(f"    {line}" for line in output) or "    (no output)"
    return Problem(
        name,
        f"`commands.{name}` (`{command}`) fails on origin/main (exit {done.returncode}). Make it "
        f"pass, commit and push the fix to main, then start again. The end of its output:\n{tail}",
    )
