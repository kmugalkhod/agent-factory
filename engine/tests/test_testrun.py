"""Tests for running a repo command (`commands.test`) in a worktree: parsing, the clean
environment, JUnit reports, the saved output and the `test_run` event."""

import os
import shlex
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from factory_engine import events as ev
from factory_engine.errors import ConfigError
from factory_engine.events import EventLog, read_events
from factory_engine.providers import BASE_VARS
from factory_engine.run import Role
from factory_engine.testrun import (
    CommandResult,
    command_env,
    detect_runner,
    parse_command,
    read_junit,
    report_args,
    run_repo_command,
    with_report_args,
)

PYTEST = f"{shlex.quote(Path(sys.executable).as_posix())} -m pytest"
CREDENTIALS = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CONFIG_DIR",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "GH_TOKEN",
    "GITHUB_TOKEN",
)
LEAKS = (*CREDENTIALS, "FACTORY_UNLISTED")  # credentials, and a variable nobody allowed
PASSING_OK = "def test_ok():\n    pass\n"


def clock() -> datetime:
    return datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def make_repo(root: Path, files: dict[str, str]) -> Path:
    """A folder with a `pytest.ini` (so pytest's rootdir is the folder) and `files`."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def run_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "run"
    folder.mkdir()
    return folder


@pytest.fixture
def log(run_folder: Path) -> EventLog:
    return EventLog(run_folder, repo="demo", run_id=1, clock=clock)


# ------------------------------------------------------------------ parsing the command


@pytest.mark.parametrize(
    ("command", "argv"),
    [
        ("pytest", ["pytest"]),
        ("uv run pytest -q", ["uv", "run", "pytest", "-q"]),
        ('pytest -k "a and not b"', ["pytest", "-k", "a and not b"]),
        ('pytest -k "x;y"', ["pytest", "-k", "x;y"]),  # quoted: an argument, not an operator
        ("pytest -k 'a && b'", ["pytest", "-k", "a && b"]),
        (
            "'C:/Program Files/py/python.exe' -m pytest",
            ["C:/Program Files/py/python.exe", "-m", "pytest"],
        ),
        ("npx vitest run", ["npx", "vitest", "run"]),
    ],
)
def test_parse_command_splits_like_a_posix_shell(command: str, argv: list[str]) -> None:
    assert parse_command(command) == argv


@pytest.mark.parametrize(
    "command",
    [
        "a && b",
        "a&&b",
        "a || b",
        "a | b",
        "a; b",
        "a > out.txt",
        "a < in.txt",
        "a 2>&1",
        "a & b",
        "echo `whoami`",
        "echo $(whoami)",
        "(cd x)",
        "a\nb",
    ],
)
def test_parse_command_refuses_shell_operators(command: str) -> None:
    with pytest.raises(ConfigError, match="script"):
        parse_command(command)


@pytest.mark.parametrize("command", ["", "   ", "pytest 'unclosed", "FOO=1 pytest"])
def test_parse_command_refuses_unusable_commands(command: str) -> None:
    with pytest.raises(ConfigError):
        parse_command(command)


# ------------------------------------------------------------------ test runners


@pytest.mark.parametrize(
    ("argv", "runner"),
    [
        (["pytest"], "pytest"),
        (["py.test", "-q"], "pytest"),
        (["pytest-3"], "pytest"),
        (["uv", "run", "pytest"], "pytest"),
        (["python", "-m", "pytest", "tests"], "pytest"),
        (["C:/venv/Scripts/pytest.exe"], "pytest"),
        (["C:\\venv\\Scripts\\py.test.EXE"], "pytest"),
        (["uv", "run", "pytest", "tests/test_pytest_helpers.py"], "pytest"),
        (["npx", "vitest", "run"], "vitest"),
        (["pnpm", "exec", "vitest"], "vitest"),
        (["node_modules/.bin/vitest.cmd", "run"], "vitest"),
        (["pnpm", "test"], None),
        (["npm", "test"], None),
        (["python", "tests/test_pytest.py"], None),
        (["cargo", "test"], None),
        # Only the command position picks the runner, never an argument.
        (["python", "-m", "unittest", "discover", "-s", "tests/pytest"], None),
        (["python", "scripts/run.py", "pytest"], None),
        (["python", "-c", "import pytest; pytest.main()"], None),
        (["python", "-m", "pytest_helper"], None),
        (["node", "scripts/vitest.js"], None),
        (["make", "pytest"], None),
        (["uv", "run", "python", "-m", "unittest", "tests/test_vitest.py"], None),
        (["uv", "sync", "pytest"], None),
        (["uv", "run", "--unknown-flag", "python", "check.py"], None),  # unclear, but no runner
        # Python's own options.
        (["python3.12", "-X", "dev", "-m", "pytest"], "pytest"),
        (["python", "-Xdev", "-Wdefault", "-m", "pytest"], "pytest"),
        (["python", "-B", "-W", "error", "-m", "pytest"], "pytest"),
        (["python", "-Bu", "-m", "pytest"], "pytest"),
        (["python", "-mpytest"], "pytest"),
        (["py", "-3.12", "-m", "pytest"], "pytest"),
        (["C:/Python312/python.exe", "-m", "pytest"], "pytest"),
        # Launchers and their options.
        (["uv", "--directory", "app", "run", "pytest"], "pytest"),
        (["uv", "--directory=app", "run", "pytest"], "pytest"),
        (["uv", "-q", "--project", ".", "run", "--frozen", "pytest", "-x"], "pytest"),
        (["uv", "run", "--frozen", "--with", "pytest-xdist", "pytest"], "pytest"),
        (["uv", "run", "--", "pytest"], "pytest"),
        (["uv", "run", "python", "-m", "pytest"], "pytest"),
        (["uv", "run", "-m", "pytest"], "pytest"),
        (["uv", "run", "--module=pytest"], "pytest"),
        (["uv", "tool", "run", "pytest"], "pytest"),
        (["uvx", "--from", "pytest==9.1", "pytest"], "pytest"),
        (["poetry", "-C", "app", "run", "pytest"], "pytest"),
        (["poetry", "run", "python", "-m", "pytest"], "pytest"),
        (["pdm", "run", "pytest"], "pytest"),
        (["pipenv", "run", "pytest"], "pytest"),
        (["npx", "--yes", "vitest"], "vitest"),
        (["npm", "exec", "--", "vitest"], "vitest"),
        (["npm", "--prefix", "web", "exec", "vitest"], "vitest"),
        (["pnpm", "vitest", "run"], "vitest"),
        (["pnpm", "--filter", "web", "exec", "vitest"], "vitest"),
        (["pnpm", "-F", "web", "exec", "vitest"], "vitest"),
        (["pnpm", "-Fweb", "exec", "vitest"], "vitest"),
        (["pnpm", "-C", "web", "dlx", "vitest"], "vitest"),
        (["yarn", "vitest"], "vitest"),
        (["yarn", "--cwd", "web", "exec", "vitest"], "vitest"),
    ],
)
def test_detect_runner(argv: list[str], runner: str | None) -> None:
    assert detect_runner(argv) == runner


@pytest.mark.parametrize(
    "argv",
    [
        ["uv", "--new-global-option", "run", "pytest"],  # takes a value, or not?
        ["uv", "run", "--new-run-option", "pytest"],
        ["poetry", "--new-option", "run", "pytest"],
        ["pnpm", "--new-option", "vitest"],
        ["npm", "run", "vitest"],  # a package script named vitest, or vitest itself?
        ["pnpm", "run", "vitest"],
        ["yarn", "run", "vitest"],
        ["npm", "run-script", "pytest"],
    ],
)
def test_a_command_form_that_cant_be_read_for_certain_is_refused(argv: list[str]) -> None:
    with pytest.raises(ConfigError, match="plain form"):
        detect_runner(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ["pytest", "-p", "no:junitxml"],
        ["pytest", "-pno:junitxml", "tests"],
        ["uv", "run", "pytest", "-x", "-p", "no:junitxml"],
        ["npx", "vitest", "run", "--outputFile=results.json"],
        ["npx", "vitest", "--outputFile.json", "out.json"],
    ],
)
def test_options_that_stop_the_report_are_refused(argv: list[str]) -> None:
    with pytest.raises(ConfigError, match="Remove that option"):
        detect_runner(argv)


R = "REPORT"  # stands for the engine's options in the expected commands below


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["pytest"], ["pytest", R]),
        (["pytest", "-x", "tests"], ["pytest", "-x", "tests", R]),
        (["pytest", "--", "tests"], ["pytest", R, "--", "tests"]),
        (["python", "-m", "pytest", "--", "tests"], ["python", "-m", "pytest", R, "--", "tests"]),
        (
            ["uv", "run", "pytest", "-x", "--", "tests"],
            ["uv", "run", "pytest", "-x", R, "--", "tests"],
        ),
        (["uv", "run", "--", "pytest", "--", "t"], ["uv", "run", "--", "pytest", R, "--", "t"]),
        (["uv", "run", "--", "pytest", "-q"], ["uv", "run", "--", "pytest", "-q", R]),
        # The user's own report options come first, so the engine's (the last given) win.
        (["pytest", "--junitxml=results.xml"], ["pytest", "--junitxml=results.xml", R]),
        (
            ["pytest", "-o", "junit_family=xunit2", "--", "tests"],
            ["pytest", "-o", "junit_family=xunit2", R, "--", "tests"],
        ),
        (["npx", "vitest", "run", "--", "src"], ["npx", "vitest", "run", R, "--", "src"]),
        (
            ["pnpm", "vitest", "--reporter=dot", "--", "src"],
            ["pnpm", "vitest", "--reporter=dot", R, "--", "src"],
        ),
        (["python", "check.py", "--", "x"], ["python", "check.py", "--", "x"]),
    ],
)
def test_report_options_go_after_the_users_and_before_the_runners_double_dash(
    tmp_path: Path, argv: list[str], expected: list[str]
) -> None:
    report, cache = tmp_path / "junit.xml", tmp_path / "cache"
    full, runner = with_report_args(argv, report, cache)
    added = report_args(runner, report, cache)
    at = expected.index(R) if R in expected else len(expected)
    assert full == [*expected[:at], *added, *expected[at + 1 :]]
    assert bool(added) == (R in expected)


def test_report_args_for_pytest_write_junit_and_keep_the_cache_out_of_the_worktree(
    tmp_path: Path,
) -> None:
    report = tmp_path / "junit.xml"
    args = report_args("pytest", report, tmp_path / "cache")
    assert f"--junitxml={report}" in args
    assert f"cache_dir={tmp_path / 'cache'}" in args


def test_report_args_for_pytest_ask_for_test_files_in_the_report(tmp_path: Path) -> None:
    args = report_args("pytest", tmp_path / "junit.xml", tmp_path / "cache")
    assert args[args.index("junit_family=xunit1") - 1] == "-o"


def test_report_args_for_vitest_write_junit(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    args = report_args("vitest", report, tmp_path / "cache")
    assert "--reporter=junit" in args
    assert f"--outputFile.junit={report}" in args


def test_report_args_for_other_runners_are_empty(tmp_path: Path) -> None:
    assert report_args(None, tmp_path / "junit.xml", tmp_path / "cache") == []


# ------------------------------------------------------------------ the environment


def test_command_env_drops_variables_nobody_allowed() -> None:
    assert "FACTORY_UNLISTED" not in command_env({"PATH": "/bin", "FACTORY_UNLISTED": "x"})


def test_command_env_keeps_the_system_basics() -> None:
    env = command_env({"PATH": "/bin", "SYSTEMROOT": "C:/Windows", "TEMP": "/tmp"})
    assert env["PATH"] == "/bin"
    assert env["SYSTEMROOT"] == "C:/Windows"
    assert env["TEMP"] == "/tmp"


@pytest.mark.parametrize("name", CREDENTIALS)
def test_command_env_drops_credentials_even_when_allowed(name: str) -> None:
    env = command_env({"PATH": "/bin", name: "secret"}, allow=(*BASE_VARS, name))
    assert name not in env
    assert "secret" not in env.values()


@pytest.mark.parametrize(
    "name", ["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"]
)
def test_command_env_refuses_credentials_in_extra(name: str) -> None:
    with pytest.raises(ConfigError, match=name):
        command_env({"PATH": "/bin"}, extra={name: "secret"})


def test_command_env_adds_extra_variables() -> None:
    env = command_env({"PATH": "/bin"}, extra={"APP_MODE": "test"})
    assert env["APP_MODE"] == "test"


def test_command_env_keeps_python_from_writing_bytecode_into_the_worktree() -> None:
    assert command_env({})["PYTHONDONTWRITEBYTECODE"] == "1"


# ------------------------------------------------------------------ JUnit reports

PYTEST_REPORT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests"><testsuite name="pytest" errors="1" failures="1" skipped="1"
 tests="5" time="0.1">
<testcase classname="tests.test_calc" name="test_add" file="tests/test_calc.py" line="3"
 time="0.001" />
<testcase classname="tests.test_calc" name="test_mul" time="0.001">
<failure message="ImportError: cannot import name 'mul'">trace</failure></testcase>
<testcase classname="tests.test_calc.TestDiv" name="test_div[1-2]" time="0.001">
<skipped type="pytest.skip" message="later">skip</skipped></testcase>
<testcase classname="" name="tests.test_broken" time="0.000">
<error message="collection failure">SyntaxError</error></testcase>
<testcase classname="tests.test_calc" name="test_sub" time="0.001" />
</testsuite></testsuites>
"""

VITEST_REPORT = """<?xml version="1.0" encoding="UTF-8" ?>
<testsuites name="vitest tests" tests="2" failures="1" errors="0" time="0.1">
<testsuite name="tests/sum.test.ts" tests="2" failures="1" errors="0" skipped="0" time="0.01">
<testcase classname="tests/sum.test.ts" name="sum &gt; adds" time="0.001"></testcase>
<testcase classname="tests/sum.test.ts" name="sum &gt; subtracts" time="0.002">
<failure message="expected 1 to be 2" type="AssertionError">trace</failure></testcase>
</testsuite></testsuites>
"""


def test_read_junit_counts_a_pytest_report(tmp_path: Path) -> None:
    path = tmp_path / "junit.xml"
    path.write_text(PYTEST_REPORT, encoding="utf-8")
    report = read_junit(path)
    assert (report.passed, report.failed, report.errors, report.skipped) == (2, 1, 1, 1)
    assert report.collection_errors == ("tests.test_broken",)
    failed = [case.id for case in report.cases if case.outcome == "failed"]
    assert failed == ["tests.test_calc::test_mul"]
    assert report.cases[0].file == "tests/test_calc.py"


def test_read_junit_counts_a_vitest_report(tmp_path: Path) -> None:
    path = tmp_path / "junit.xml"
    path.write_text(VITEST_REPORT, encoding="utf-8")
    report = read_junit(path)
    assert (report.passed, report.failed, report.errors, report.skipped) == (1, 1, 0, 0)
    assert report.cases[1].id == "tests/sum.test.ts::sum > subtracts"


def test_read_junit_counts_suite_errors_no_test_case_carries(tmp_path: Path) -> None:
    path = tmp_path / "junit.xml"
    path.write_text('<testsuite name="x" errors="2" tests="0"></testsuite>', encoding="utf-8")
    assert read_junit(path).errors == 2


@pytest.mark.parametrize("text", ["", "<testsuite", "not xml at all"])
def test_read_junit_raises_on_a_broken_report(tmp_path: Path, text: str) -> None:
    path = tmp_path / "junit.xml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="junit.xml"):
        read_junit(path)


# ------------------------------------------------------------------ running pytest for real


def run(
    repo: Path,
    run_folder: Path,
    log: EventLog,
    *,
    command: str = PYTEST,
    stage: Role = "tester",
    attempt: int = 1,
    parent_env: dict[str, str] | None = None,
    allow: tuple[str, ...] = BASE_VARS,
    timeout: float = 120.0,
) -> CommandResult:
    return run_repo_command(
        command,
        worktree=repo,
        run_folder=run_folder,
        stage=stage,
        attempt=attempt,
        parent_env=dict(os.environ) if parent_env is None else parent_env,
        allow=allow,
        events=log,
        timeout=timeout,
    )


def test_passing_tests_exit_0_with_counts(tmp_path: Path, run_folder: Path, log: EventLog) -> None:
    repo = make_repo(tmp_path / "repo", {"tests/test_ok.py": "def test_ok():\n    assert True\n"})
    result = run(repo, run_folder, log)
    assert result.exit_code == 0
    assert not result.timed_out
    assert result.runner == "pytest"
    assert result.report is not None
    assert (result.report.passed, result.report.failed) == (1, 0)


def test_the_report_names_each_test_file(tmp_path: Path, run_folder: Path, log: EventLog) -> None:
    repo = make_repo(tmp_path / "repo", {"tests/test_ok.py": PASSING_OK})
    result = run(repo, run_folder, log)
    assert result.report is not None
    files = [case.file.replace("\\", "/") for case in result.report.cases]
    assert files == ["tests/test_ok.py"]


def test_a_relative_worktree_and_run_folder_still_get_a_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_repo(tmp_path / "repo", {"tests/test_ok.py": PASSING_OK})
    (tmp_path / "run").mkdir()
    log = EventLog(tmp_path / "run", repo="demo", run_id=1, clock=clock)
    monkeypatch.chdir(tmp_path)
    result = run(Path("repo"), Path("run"), log)
    assert result.report is not None, result.report_problem
    assert result.report.passed == 1
    assert result.output_path == (tmp_path / "run" / "test-output-tester-1.txt").resolve()
    assert (tmp_path / "run" / "junit-tester-1.xml").is_file()
    assert not (tmp_path / "repo" / "run").exists()


def test_failing_tests_are_reported_by_name(
    tmp_path: Path, run_folder: Path, log: EventLog
) -> None:
    repo = make_repo(
        tmp_path / "repo",
        {"tests/test_bad.py": "def test_ok():\n    pass\n\ndef test_bad():\n    assert 1 == 2\n"},
    )
    result = run(repo, run_folder, log)
    assert result.exit_code == 1
    assert result.report is not None
    failed = [case.id for case in result.report.cases if case.outcome == "failed"]
    assert failed == ["tests.test_bad::test_bad"]


def test_the_output_is_saved_to_the_run_folder(
    tmp_path: Path, run_folder: Path, log: EventLog
) -> None:
    repo = make_repo(
        tmp_path / "repo", {"tests/test_bad.py": "def test_bad():\n    assert 1 == 2\n"}
    )
    result = run(repo, run_folder, log, stage="builder", attempt=2)
    assert result.output_path == run_folder / "test-output-builder-2.txt"
    text = result.output_path.read_text(encoding="utf-8")
    assert "test_bad" in text
    assert "1 failed" in text


def test_a_test_run_event_is_logged(tmp_path: Path, run_folder: Path, log: EventLog) -> None:
    repo = make_repo(
        tmp_path / "repo",
        {"tests/test_mix.py": "def test_ok():\n    pass\n\ndef test_bad():\n    assert 0\n"},
    )
    run(repo, run_folder, log)
    [event] = read_events(run_folder)
    assert event.role == "tester"
    assert isinstance(event.body, ev.TestRun)
    assert event.body.command == PYTEST
    assert (event.body.exit_code, event.body.passed, event.body.failed) == (1, 1, 1)


@pytest.mark.parametrize(
    "source",
    [
        "import no_such_module_for_factory_tests\n\ndef test_x():\n    pass\n",  # collection
        "def test_x(:\n    pass\n",  # syntax error
    ],
)
def test_collection_and_syntax_errors_are_reported(
    tmp_path: Path, run_folder: Path, log: EventLog, source: str
) -> None:
    repo = make_repo(tmp_path / "repo", {"tests/test_broken.py": source})
    result = run(repo, run_folder, log)
    assert result.exit_code == 2
    assert result.report is not None
    assert result.report.collection_errors == ("tests.test_broken",)


def test_a_stale_report_never_stands_in_for_a_new_run(
    tmp_path: Path, run_folder: Path, log: EventLog
) -> None:
    (run_folder / "junit-tester-1.xml").write_text(VITEST_REPORT, encoding="utf-8")
    repo = make_repo(tmp_path / "repo", {"tests/test_ok.py": "def test_ok():\n    pass\n"})
    script = repo / "check.py"
    script.write_text("raise SystemExit(0)\n", encoding="utf-8")
    command = f"{shlex.quote(Path(sys.executable).as_posix())} check.py"
    result = run(repo, run_folder, log, command=command)
    assert result.runner is None
    assert result.report is None
    assert not (run_folder / "junit-tester-1.xml").exists()


def test_other_runners_report_the_exit_code_only(
    tmp_path: Path, run_folder: Path, log: EventLog
) -> None:
    repo = make_repo(tmp_path / "repo", {"check.py": "print('checking')\nraise SystemExit(3)\n"})
    command = f"{shlex.quote(Path(sys.executable).as_posix())} check.py"
    result = run(repo, run_folder, log, command=command)
    assert result.exit_code == 3
    assert result.report is None
    assert "checking" in result.output_path.read_text(encoding="utf-8")


def test_a_timeout_stops_the_command(tmp_path: Path, run_folder: Path, log: EventLog) -> None:
    repo = make_repo(
        tmp_path / "repo",
        {"tests/test_slow.py": "import time\n\ndef test_slow():\n    time.sleep(60)\n"},
    )
    started = time.monotonic()
    result = run(repo, run_folder, log, timeout=1.0)
    assert result.timed_out
    assert result.exit_code is None
    assert time.monotonic() - started < 50
    assert "timeout" in result.output_path.read_text(encoding="utf-8")
    [event] = read_events(run_folder)
    assert isinstance(event.body, ev.TestRun)
    assert event.body.exit_code == -1


def test_credentials_never_reach_the_command(
    tmp_path: Path, run_folder: Path, log: EventLog
) -> None:
    probe = (
        "import os\n\n"
        f"LEAKS = {LEAKS!r}\n\n"
        "def test_no_credentials():\n"
        "    assert [name for name in LEAKS if name in os.environ] == []\n\n"
        "def test_system_basics_are_there():\n"
        "    assert os.environ.get('PATH')\n"
    )
    repo = make_repo(tmp_path / "repo", {"tests/test_env.py": probe})
    parent = dict(os.environ) | {name: "secret-value" for name in LEAKS}
    result = run(repo, run_folder, log, parent_env=parent, allow=(*BASE_VARS, "GH_TOKEN"))
    assert result.exit_code == 0, result.output
    assert result.report is not None
    assert result.report.passed == 2


def test_shell_operators_are_refused_before_anything_runs(
    tmp_path: Path, run_folder: Path, log: EventLog
) -> None:
    repo = make_repo(tmp_path / "repo", {})
    with pytest.raises(ConfigError, match="script"):
        run(repo, run_folder, log, command=f"{PYTEST} && echo done")
    assert read_events(run_folder) == []
    assert list(run_folder.iterdir()) == []


def test_a_missing_program_is_a_config_error(
    tmp_path: Path, run_folder: Path, log: EventLog
) -> None:
    repo = make_repo(tmp_path / "repo", {})
    with pytest.raises(ConfigError, match="no-such-program-for-factory"):
        run(repo, run_folder, log, command="no-such-program-for-factory --version")
