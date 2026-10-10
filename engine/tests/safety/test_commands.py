"""Tests for factory_engine.safety.commands (task 2.9): the Bash and PowerShell check."""

import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from factory_engine.config import Settings, default_settings
from factory_engine.run import Role
from factory_engine.safety.commands import CommandDecision, ShellPolicy, check_shell_command

WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="Windows path rules")
ROLES: tuple[Role, ...] = ("planner", "tester", "builder", "reviewer")
COMMANDS = {"test": "uv run pytest", "build": "npm run build", "lint": "uv run ruff check ."}
TOOLS = {"sh": "Bash", "ps": "PowerShell"}


def _settings() -> Settings:
    base = default_settings("win32")
    return base.model_copy(update={"commands": base.commands.model_copy(update=COMMANDS)})


@pytest.fixture
def roots(tmp_path: Path) -> dict[str, Path]:
    worktree = tmp_path / "worktree"
    (worktree / "src").mkdir(parents=True)
    (worktree / "src" / "app.py").write_text("def main(): ...\n", encoding="utf-8")
    (worktree / "config").mkdir()
    (worktree / "config" / ".env.local").write_text("TOKEN=secret\n", encoding="utf-8")
    run = tmp_path / "run"
    run.mkdir()
    (run / "plan.md").write_text("# Plan\n", encoding="utf-8")
    outside = tmp_path / "outside_folder"
    outside.mkdir()
    (outside / "x").write_text("x", encoding="utf-8")
    return {"wt": worktree, "run": run, "out": outside}


def _check(
    roots: dict[str, Path], role: Role, shell: str, command: object, cwd: Path | None = None
) -> CommandDecision:
    policy = ShellPolicy.build(role, _settings(), worktree=roots["wt"], run_folder=roots["run"])
    return check_shell_command(policy, TOOLS[shell], {"command": command}, cwd=cwd)


def _assert(decision: CommandDecision, allowed: bool, reason: str) -> None:
    assert decision.allowed is allowed, decision.reason
    if not allowed:
        assert decision.reason.strip()
        assert reason.lower() in decision.reason.lower(), decision.reason


# Each case: role, shell ("sh" Bash, "ps" PowerShell), command, allowed, a word the reason has.
CASES = [
    # ---- the builder's allowlist: test, build, lint
    ("builder", "sh", "uv run pytest", True, ""),
    ("builder", "sh", "uv run pytest tests/test_x.py -q", True, ""),
    ("builder", "sh", "uv run ruff check .", True, ""),
    ("builder", "sh", "npm run build", True, ""),
    ("builder", "sh", "uv run pytest && uv run ruff check .", True, ""),
    ("builder", "sh", "cd src && uv run pytest", True, ""),
    ("builder", "sh", "uv run pytest 2>&1", True, ""),
    ("builder", "sh", "uv run pytest > /dev/null 2>&1", True, ""),
    ("builder", "sh", "uv run pytest -k '$HOME'", True, ""),
    ("builder", "sh", "uv run pytest # then git push", True, ""),
    ("builder", "ps", "uv run pytest > $null", True, ""),
    ("builder", "ps", "uv run pytest; uv run ruff check .", True, ""),
    # ---- compound commands: every part is checked
    ("builder", "sh", "uv run pytest | tail -5", False, "isn't allowed"),
    ("builder", "sh", "uv run pytest; rm -rf ..", False, "parent"),
    ("builder", "sh", "uv run pytest && curl https://example.com", False, "network"),
    ("builder", "sh", "uv run pytest || wget http://example.com/x", False, "network"),
    ("builder", "sh", "(uv run pytest) && git push", False, "git"),
    ("builder", "sh", "uv run pytest\ngit push", False, "git"),
    ("builder", "sh", "uv run pytest & git push", False, "git"),
    ("builder", "sh", "uv run python -c 'print(1)'", False, "isn't allowed"),
    ("builder", "sh", "pip install requests", False, "isn't allowed"),
    ("builder", "sh", "rm src/app.py", False, "isn't allowed"),
    # ---- redirects and variables
    ("builder", "sh", "uv run pytest > out.txt", False, "redirect"),
    ("builder", "sh", "uv run pytest >> ../outside_folder/log", False, "redirect"),
    ("builder", "sh", "cat < /etc/passwd", False, "redirect"),
    ("builder", "sh", "uv run pytest <<EOF", False, "redirect"),
    ("builder", "sh", "FOO=1 uv run pytest", False, "variable"),
    ("builder", "sh", "uv run pytest $ARGS", False, "expansion"),
    ("builder", "sh", "uv run pytest ${ARGS}", False, "expansion"),
    ("builder", "sh", "uv run pytest $(git log)", False, "expansion"),
    ("builder", "sh", "uv run pytest `git log`", False, "expansion"),
    ("builder", "sh", 'uv run pytest "$HOME"', False, "expansion"),
    ("builder", "sh", "uv run pytest %USERPROFILE%", False, "expansion"),
    ("builder", "ps", "uv run pytest $env:USERPROFILE", False, "expansion"),
    ("builder", "ps", "uv run pytest @args", False, "expansion"),
    ("builder", "ps", "uv run pytest `; git push", False, "backtick"),
    # ---- git, however it is written
    ("builder", "sh", "g\\it log", False, "git"),
    ("builder", "sh", '"git" log', False, "git"),
    ("builder", "sh", "g''it status", False, "git"),
    ("builder", "sh", "'g'\"i\"t diff", False, "git"),
    ("builder", "sh", "GIT log", False, "git"),
    ("builder", "sh", "/usr/bin/git log", False, "git"),
    ("builder", "sh", "g{i,}t log", False, "brace"),
    ("builder", "sh", "gh pr create", False, "git"),
    ("builder", "ps", "& 'C:\\Program Files\\Git\\bin\\git.exe' log", False, "git"),
    ("builder", "ps", "Git.EXE status", False, "git"),
    # ---- other shells and wrappers
    ("builder", "sh", "cmd /c git log", False, "git"),
    ("builder", "sh", 'cmd /c "uv run pytest"', False, "directly"),
    ("builder", "sh", "bash -c 'git log'", False, "git"),
    ("builder", "sh", "sh -c 'curl https://example.com'", False, "network"),
    ("builder", "sh", "env git log", False, "git"),
    ("builder", "sh", "xargs git log", False, "git"),
    ("builder", "ps", 'powershell -c "git status"', False, "git"),
    ("builder", "ps", "pwsh -Command Invoke-WebRequest https://example.com", False, "network"),
    ("builder", "ps", "powershell -EncodedCommand ZwBpAHQAIABsAG8AZwA=", False, "encoded"),
    ("builder", "ps", "Invoke-Expression 'git log'", False, "git"),
    ("builder", "ps", 'iex "git log"', False, "git"),
    ("builder", "ps", "& git log", False, "git"),
    # ---- network
    ("builder", "ps", "Invoke-WebRequest https://example.com", False, "network"),
    ("builder", "ps", "iwr https://example.com -OutFile a.zip", False, "network"),
    ("builder", "ps", "irm https://example.com", False, "network"),
    ("builder", "sh", "curl.exe -O https://example.com/x", False, "network"),
    # ---- cd out of the worktree
    ("builder", "sh", "cd ..", False, "worktree"),
    ("builder", "sh", "cd ../..", False, "worktree"),
    ("builder", "sh", "cd src/../..", False, "worktree"),
    ("builder", "sh", "cd", False, "worktree"),
    ("builder", "sh", "cd ~", False, "worktree"),
    ("builder", "sh", "cd {out}", False, "worktree"),
    ("builder", "sh", "cd src && cd ../..", False, "worktree"),
    ("builder", "sh", "(cd src) ; cd ..", False, "worktree"),
    ("builder", "ps", "Set-Location ..", False, "worktree"),
    ("builder", "ps", "pushd ..", False, "worktree"),
    # ---- deletes on parent folders
    ("builder", "sh", "rm -rf ../", False, "parent"),
    ("builder", "sh", "rm -rf /", False, "parent"),
    ("builder", "sh", "rm -rf .", False, "parent"),
    ("builder", "sh", "rmdir src/../..", False, "parent"),
    ("builder", "ps", "uv run pytest; Remove-Item -Recurse -Force ..", False, "parent"),
    ("builder", "ps", "del ../outside_folder/x", False, "parent"),
    # ---- the tester: test and lint only
    ("tester", "sh", "uv run pytest", True, ""),
    ("tester", "sh", "uv run ruff check .", True, ""),
    ("tester", "sh", "npm run build", False, "isn't allowed"),
    ("tester", "sh", "ls", False, "isn't allowed"),
    # ---- the planner: read-only commands only
    ("planner", "sh", "ls src", True, ""),
    ("planner", "sh", "pwd", True, ""),
    ("planner", "sh", "cat src/app.py | head -5", True, ""),
    ("planner", "sh", "grep -rn def src", True, ""),
    ("planner", "sh", "find . -name '*.py'", True, ""),
    ("planner", "sh", "cd src && ls", True, ""),
    ("planner", "sh", "cat {run}/plan.md", True, ""),
    ("planner", "ps", "Get-ChildItem -Recurse src", True, ""),
    ("planner", "ps", "Get-Content src/app.py | Select-String def", True, ""),
    ("planner", "sh", "find . -name '*.py' -delete", False, "read-only"),
    ("planner", "sh", "find . -exec rm '{}' ';'", False, "read-only"),
    ("planner", "sh", "rg --pre cat def src", False, "read-only"),
    ("planner", "sh", "sort -o out.txt src/app.py", False, "read-only"),
    ("planner", "sh", "uv run pytest", False, "isn't allowed"),
    ("planner", "sh", "rm src/app.py", False, "isn't allowed"),
    ("planner", "sh", "touch src/new.py", False, "isn't allowed"),
    ("planner", "sh", "cat ../outside_folder/x", False, "outside"),
    ("planner", "sh", "cat {out}/x", False, "outside"),
    ("planner", "sh", "cat ~/.ssh/id_rsa", False, "outside"),
    ("planner", "sh", "cat config/.env.local", False, "secret"),
    ("planner", "sh", "grep -r TOKEN .", False, "secret"),
    ("planner", "sh", "grep -r TOKEN", False, "secret"),
    ("planner", "sh", "git log", False, "git"),
    # ---- review round 1: wildcards the shell expands (issue 1)
    ("planner", "sh", "cat config/.en[v].local", False, "secret"),
    ("planner", "sh", "cat config/.env*", False, "secret"),
    ("planner", "sh", "cat config/*", False, "secret"),
    ("planner", "sh", "cat ../outside_folder/*", False, "outside"),
    ("planner", "sh", "cat ../*/x", False, "outside"),
    ("planner", "sh", "cat src/*.py", True, ""),
    ("planner", "sh", "cat 'config/.en[v].local'", True, ""),
    ("planner", "ps", "Get-Content 'config/.en[v].local'", False, "secret"),
    ("builder", "sh", "cd ../*", False, "worktree"),
    # ---- attached and combined short flags (issue 2)
    ("planner", "sh", "sort -ooutput.txt src/app.py", False, "read-only"),
    ("planner", "sh", "sort -uo out.txt src/app.py", False, "read-only"),
    ("planner", "sh", "sort --output=out.txt src/app.py", False, "read-only"),
    ("planner", "sh", "tree -oout.txt src", False, "read-only"),
    ("planner", "sh", "sort -n src/app.py", True, ""),
    ("planner", "ps", "sort -Property Name", True, ""),
    # ---- programs named by path (issue 4)
    ("planner", "sh", "./cat src/app.py", False, "plain name"),
    ("planner", "sh", "src/cat src/app.py", False, "plain name"),
    ("planner", "ps", "& .\\cat.ps1 src/app.py", False, "plain name"),
    ("planner", "ps", "cat.ps1 src/app.py", False, "plain name"),
    ("builder", "sh", "./uv run pytest", False, "isn't allowed"),
    ("builder", "ps", ".\\uv.exe run pytest", False, "isn't allowed"),
    # ---- paths inside options (issue 5)
    ("planner", "sh", "diff --from-file=../outside_folder/x src/app.py", False, "outside"),
    ("planner", "sh", "diff --from-file=config/.env.local src/app.py", False, "secret"),
    ("planner", "sh", "grep -f../outside_folder/x def src", False, "outside"),
    ("planner", "ps", "Get-Content -Path:../outside_folder/x", False, "outside"),
    ("planner", "sh", "head -n5 src/app.py", True, ""),
    ("planner", "sh", "grep -rn --include=*.py def src", True, ""),
    # ---- cd: returning to the worktree, subshells and pipes (issue 8)
    ("builder", "sh", "cd src && cd ..", True, ""),
    ("builder", "sh", "cd src; cd ..", True, ""),
    ("builder", "sh", "cd src && cd .. && uv run pytest", True, ""),
    ("builder", "sh", "cd missing; cd ..", False, "worktree"),
    ("builder", "sh", "(cd src && cd ..) && cd ..", False, "worktree"),
    ("builder", "ps", "(cd src); cd ..", False, "worktree"),
    ("planner", "sh", "cd src | ls; cd ..", False, "worktree"),
    ("builder", "sh", "uv run pytest)", False, "parentheses"),
    # ---- the reviewer: no shell at all
    ("reviewer", "sh", "ls", False, "no shell"),
    ("reviewer", "sh", "uv run pytest", False, "no shell"),
    ("reviewer", "ps", "Get-ChildItem", False, "no shell"),
    # ---- bad input
    ("builder", "sh", "", False, "command"),
    ("builder", "sh", 42, False, "command"),
    ("builder", "sh", "uv run pytest 'unclosed", False, "quote"),
]

WINDOWS_CASES = [
    ("builder", "sh", "cd /c/Windows", False, "worktree"),
    ("builder", "ps", "sl ..\\..", False, "worktree"),
    ("builder", "ps", "cd C:\\", False, "worktree"),
    ("builder", "sh", "cd SRC && uv run pytest", True, ""),
    ("planner", "ps", "Get-Content ..\\outside_folder\\x", False, "outside"),
    ("planner", "ps", "type \\\\server\\share\\x", False, "outside"),
    ("builder", "ps", "uv run pytest > nul", True, ""),
    ("planner", "ps", "cat.exe src/app.py", True, ""),
]


def _params(cases: Sequence[tuple[str, str, object, bool, str]]) -> list[object]:
    return [pytest.param(*case, id=f"{case[0]}-{i}") for i, case in enumerate(cases)]


def _fill(command: object, roots: dict[str, Path]) -> object:
    if not isinstance(command, str):
        return command
    for key in ("out", "run", "wt"):
        command = command.replace("{" + key + "}", roots[key].as_posix())
    return command


@pytest.mark.parametrize(("role", "shell", "command", "allowed", "reason"), _params(CASES))
def test_shell_rules(
    roots: dict[str, Path], role: Role, shell: str, command: object, allowed: bool, reason: str
) -> None:
    _assert(_check(roots, role, shell, _fill(command, roots)), allowed, reason)


@WINDOWS
@pytest.mark.parametrize(("role", "shell", "command", "allowed", "reason"), _params(WINDOWS_CASES))
def test_windows_shell_rules(
    roots: dict[str, Path], role: Role, shell: str, command: object, allowed: bool, reason: str
) -> None:
    _assert(_check(roots, role, shell, _fill(command, roots)), allowed, reason)


def test_table_has_at_least_50_cases() -> None:
    assert len(CASES) + len(WINDOWS_CASES) >= 50


GIT_COMMANDS = ["git log", "git status", "git diff", "git show HEAD", "git --version"]


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("command", GIT_COMMANDS)
@pytest.mark.parametrize("shell", ["sh", "ps"])
def test_every_role_is_denied_every_git_command(
    roots: dict[str, Path], role: Role, command: str, shell: str
) -> None:
    _assert(_check(roots, role, shell, command), False, "git" if role != "reviewer" else "")


@pytest.mark.parametrize(
    "command", ["ls", "pwd", "cat src/app.py", "uv run pytest", "npm run build", "echo hi"]
)
@pytest.mark.parametrize("shell", ["sh", "ps"])
def test_the_reviewer_is_denied_every_command(
    roots: dict[str, Path], command: str, shell: str
) -> None:
    _assert(_check(roots, "reviewer", shell, command), False, "no shell")


def test_the_shell_starts_in_the_given_folder(roots: dict[str, Path]) -> None:
    src = roots["wt"] / "src"
    _assert(_check(roots, "builder", "sh", "cd ..", cwd=src), True, "")
    _assert(_check(roots, "builder", "sh", "cd ../..", cwd=src), False, "worktree")


def test_a_shell_outside_the_worktree_is_blocked(roots: dict[str, Path]) -> None:
    _assert(_check(roots, "builder", "sh", "uv run pytest", cwd=roots["out"]), False, "worktree")


def test_a_tool_that_is_not_a_shell_is_blocked(roots: dict[str, Path]) -> None:
    policy = ShellPolicy.build(
        "builder", _settings(), worktree=roots["wt"], run_folder=roots["run"]
    )
    _assert(check_shell_command(policy, "Read", {"file_path": "x"}), False, "shell")


# ------------------------------------------------- null devices (issue 3)


@pytest.mark.parametrize(
    ("shell", "target", "windows", "allowed"),
    [
        ("sh", "/dev/null", False, True),
        ("sh", "/dev/null", True, True),
        ("sh", "nul", False, False),
        ("sh", "nul", True, False),  # Git Bash: `nul` isn't the device there
        ("sh", "/DEV/NULL", False, False),
        ("ps", "$null", False, True),
        ("ps", "$NULL", True, True),
        ("ps", "nul", True, True),
        ("ps", "NUL", True, True),
        ("ps", "nul", False, False),
        ("ps", "/dev/null", False, True),
        ("ps", "/dev/null", True, False),
    ],
)
def test_the_null_device_depends_on_shell_and_platform(
    roots: dict[str, Path], shell: str, target: str, windows: bool, allowed: bool
) -> None:
    policy = ShellPolicy.build(
        "planner", _settings(), worktree=roots["wt"], run_folder=roots["run"], windows=windows
    )
    decision = check_shell_command(policy, TOOLS[shell], {"command": f"pwd > {target}"})
    _assert(decision, allowed, "redirect")


# ---------------------------------- links a content read follows (issue 6)


def _junction(link: Path, target: Path) -> None:
    import _winapi  # Windows only; the stdlib's own junction call

    _winapi.CreateJunction(str(target), str(link))


@WINDOWS
@pytest.mark.parametrize("command", ["grep -R TOKEN src", "cat src/link/x", "grep -r x"])
def test_a_read_through_a_link_out_of_the_worktree_is_blocked(
    roots: dict[str, Path], command: str
) -> None:
    (roots["wt"] / "config" / ".env.local").unlink()
    _junction(roots["wt"] / "src" / "link", roots["out"])
    _assert(_check(roots, "planner", "sh", command), False, "outside")


@WINDOWS
def test_a_read_through_a_link_to_a_secrets_folder_is_blocked(roots: dict[str, Path]) -> None:
    _junction(roots["wt"] / "src" / "cfg", roots["wt"] / "config")
    _assert(_check(roots, "planner", "sh", "grep -R TOKEN src"), False, "secret")


@WINDOWS
def test_a_read_through_a_link_inside_the_worktree_is_allowed(roots: dict[str, Path]) -> None:
    (roots["wt"] / "lib").mkdir()
    _junction(roots["wt"] / "src" / "lib", roots["wt"] / "lib")
    _assert(_check(roots, "planner", "sh", "grep -R def src"), True, "")


def test_a_symlink_to_a_secrets_file_is_blocked(roots: dict[str, Path]) -> None:
    try:
        (roots["wt"] / "src" / "notes.txt").symlink_to(roots["wt"] / "config" / ".env.local")
    except OSError as err:
        pytest.skip(f"can't create symlinks here: {err}")
    _assert(_check(roots, "planner", "sh", "grep -R TOKEN src"), False, "secret")


# --------------------------- configured commands per shell (issue 7)


def _policy_with_test(roots: dict[str, Path], test: str) -> ShellPolicy:
    base = _settings()
    settings = base.model_copy(update={"commands": base.commands.model_copy(update={"test": test})})
    return ShellPolicy.build("builder", settings, worktree=roots["wt"], run_folder=roots["run"])


@pytest.mark.parametrize(
    ("tool", "command", "allowed"),
    [
        ("PowerShell", "python scripts\\test.py", True),
        ("PowerShell", "python scripts\\test.py -q", True),
        ("PowerShell", "python scriptstest.py", False),
        ("Bash", "python scripts\\test.py", True),
        ("Bash", "python scriptstest.py", True),  # what Bash itself runs for both
        ("Bash", "python 'scripts\\test.py'", False),
    ],
)
def test_configured_commands_are_read_with_the_calls_shell(
    roots: dict[str, Path], tool: str, command: str, allowed: bool
) -> None:
    policy = _policy_with_test(roots, "python scripts\\test.py")
    decision = check_shell_command(policy, tool, {"command": command})
    _assert(decision, allowed, "isn't allowed")


def test_a_configured_command_a_shell_cant_read_allows_nothing(roots: dict[str, Path]) -> None:
    policy = _policy_with_test(roots, "pytest $ARGS")
    _assert(check_shell_command(policy, "Bash", {"command": "pytest"}), False, "")
