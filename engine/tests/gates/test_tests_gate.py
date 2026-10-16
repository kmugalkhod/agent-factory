"""Tests for the tester gate: real pytest runs on small fixture repos made in `tmp_path`."""

import json
import os
import shlex
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from factory_engine import events as ev
from factory_engine.config import RoleSettings, default_settings
from factory_engine.errors import ConfigError
from factory_engine.events import EventLog, read_events
from factory_engine.gates.tests_gate import (
    GATE,
    TESTER_FILES,
    TesterGateOutcome,
    belongs,
    check_tests,
    load_tester_files,
    tester_files,
)
from factory_engine.git_ops import Git

PYTHON = shlex.quote(Path(sys.executable).as_posix())
PYTEST = f"{PYTHON} -m pytest"

BASE_FILES = {
    "pytest.ini": "[pytest]\npythonpath = src\n",
    ".gitignore": "__pycache__/\n.pytest_cache/\n",
    "src/calc.py": "def add(a, b):\n    return a + b\n",
    "tests/test_calc.py": "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
}
# A new test that fails for the right reason: the feature (`mul`) doesn't exist yet.
FAILING_MUL = "def test_mul():\n    from calc import mul\n\n    assert mul(2, 3) == 6\n"
PASSING_ADD = "from calc import add\n\n\ndef test_add_zero():\n    assert add(0, 0) == 0\n"
# Broken: the missing feature is imported at the top, so the file can't even be collected.
COLLECTION_ERROR = "from calc import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n"
SYNTAX_ERROR = "def test_mul(:\n    pass\n"


def clock() -> datetime:
    return datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class World:
    """A fixture repo with one commit (the base the tester started from) and a run folder."""

    def __init__(self, tmp_path: Path, git_env: dict[str, str], base: dict[str, str]) -> None:
        self.git = Git(git_env)
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        self.run_folder = tmp_path / "run"
        self.run_folder.mkdir()
        write(self.repo, base)
        self.git.run(["init", "-q", "-b", "main"], self.repo)
        self.git.run(["config", "core.autocrlf", "false"], self.repo)
        self.git.run(["add", "--all"], self.repo)
        self.git.run(["commit", "-q", "-m", "base"], self.repo)
        self.base = self.git.out(["rev-parse", "HEAD"], self.repo)
        self.log = EventLog(self.run_folder, repo="demo", run_id=1, clock=clock)
        self.tester: RoleSettings = default_settings(sys.platform).roles.tester

    def check(
        self, command: str | None = PYTEST, *, attempt: int = 1, timeout: float = 120.0
    ) -> TesterGateOutcome:
        return check_tests(
            self.git,
            worktree=self.repo,
            run_folder=self.run_folder,
            base=self.base,
            tester=self.tester,
            command=command,
            attempt=attempt,
            events=self.log,
            parent_env=dict(os.environ),
            timeout=timeout,
        )


@pytest.fixture
def world(tmp_path: Path, git_env: dict[str, str]) -> World:
    return World(tmp_path, git_env, BASE_FILES)


# ------------------------------------------------------------------ the decisions


@pytest.mark.parametrize(
    ("base_extra", "tester_files", "kind"),
    [
        pytest.param({}, {}, "no_new_tests", id="no-new-tests"),
        pytest.param(
            {},
            {"src/mul.py": "def mul(a, b):\n    return a * b\n"},
            "no_new_tests",
            id="only-outside-the-tester-paths",
        ),
        pytest.param({}, {"tests/test_more.py": PASSING_ADD}, "tests_pass", id="new-tests-pass"),
        pytest.param(
            {},
            {"tests/test_mul.py": "import pytest\n\n\n@pytest.mark.xfail\n" + FAILING_MUL},
            "tests_pass",
            id="new-tests-only-xfail",
        ),
        pytest.param(
            {}, {"tests/test_mul.py": FAILING_MUL}, "passed", id="new-tests-fail-correctly"
        ),
        pytest.param(
            {},
            {"tests/test_mul.py": FAILING_MUL + "\n\n" + PASSING_ADD},
            "passed",
            id="new-tests-fail-next-to-a-regression-test",
        ),
        pytest.param(
            {},
            {"tests/test_calc.py": BASE_FILES["tests/test_calc.py"] + "\n\n" + FAILING_MUL},
            "passed",
            id="new-test-in-an-existing-file",
        ),
        pytest.param(
            {},
            {"tests/test_mul.py": COLLECTION_ERROR},
            "broken_tests",
            id="collection-error",
        ),
        pytest.param(
            {},
            {"tests/test_mul.py": SYNTAX_ERROR},
            "broken_tests",
            id="syntax-error",
        ),
        pytest.param(
            {},
            {"tests/test_mul.py": "def test_mul(no_such_fixture):\n    assert False\n"},
            "broken_tests",
            id="missing-fixture",
        ),
        pytest.param(
            {"tests/test_old.py": "def test_old():\n    assert 1 == 2\n"},
            {"tests/test_mul.py": FAILING_MUL},
            "other_tests_fail",
            id="an-existing-test-fails",
        ),
    ],
)
def test_gate_decision(
    tmp_path: Path,
    git_env: dict[str, str],
    base_extra: dict[str, str],
    tester_files: dict[str, str],
    kind: str,
) -> None:
    world = World(tmp_path, git_env, BASE_FILES | base_extra)
    write(world.repo, tester_files)
    outcome = world.check()
    assert outcome.kind == kind, outcome.reason
    assert outcome.passed == (kind == "passed")


def test_no_new_tests_fails_without_running_the_tests(world: World) -> None:
    outcome = world.check()
    assert not outcome.passed
    assert outcome.reason.startswith("no new tests")
    assert outcome.run is None
    assert read_events(world.run_folder) == []


def test_correctly_failing_new_tests_pass_and_are_named(world: World) -> None:
    write(world.repo, {"tests/test_mul.py": FAILING_MUL + "\n\n" + PASSING_ADD})
    outcome = world.check()
    assert outcome.passed, outcome.reason
    assert outcome.failing_new == ("tests.test_mul::test_mul",)
    assert outcome.passing_new == ("tests.test_mul::test_add_zero",)


def test_passing_new_tests_fail_with_tests_pass_before_the_build(world: World) -> None:
    write(world.repo, {"tests/test_more.py": PASSING_ADD})
    outcome = world.check()
    assert outcome.reason.startswith("tests pass before the build")
    assert "tests.test_more::test_add_zero" in outcome.reason


@pytest.mark.parametrize("source", [COLLECTION_ERROR, SYNTAX_ERROR])
def test_broken_tests_are_not_called_failing_tests(world: World, source: str) -> None:
    write(world.repo, {"tests/test_mul.py": source})
    outcome = world.check()
    assert outcome.reason.startswith("broken tests")
    assert "tests fail" not in outcome.reason
    assert "tests.test_mul" in outcome.reason


def test_the_test_output_is_saved_to_the_run_folder(world: World) -> None:
    write(world.repo, {"tests/test_mul.py": FAILING_MUL})
    outcome = world.check(attempt=2)
    assert outcome.run is not None
    output = world.run_folder / "test-output-tester-2.txt"
    assert outcome.run.output_path == output
    assert "test_mul" in output.read_text(encoding="utf-8")


def test_a_test_run_event_is_logged_and_the_gate_result_is_returned(world: World) -> None:
    write(world.repo, {"tests/test_mul.py": FAILING_MUL})
    outcome = world.check()
    [event] = read_events(world.run_folder)
    assert isinstance(event.body, ev.TestRun)
    assert event.role == "tester"
    assert (event.body.passed, event.body.failed) == (1, 1)
    result = outcome.event()
    assert isinstance(result, ev.GateResult)
    assert (result.gate, result.passed, result.reason) == (GATE, True, outcome.reason)


def test_the_tester_files_are_recorded_with_their_blob_ids(world: World) -> None:
    write(world.repo, {"tests/test_mul.py": FAILING_MUL, "tests/helpers/data.txt": "1 2 3\n"})
    outcome = world.check()
    expected = {
        rel: world.git.out(["hash-object", "--", rel], world.repo)
        for rel in ("tests/helpers/data.txt", "tests/test_mul.py")
    }
    assert dict(outcome.files) == expected
    assert load_tester_files(world.run_folder) == expected
    saved = json.loads((world.run_folder / TESTER_FILES).read_text(encoding="utf-8"))
    assert saved == {"files": expected}


def test_files_outside_the_tester_paths_and_bytecode_are_not_recorded(world: World) -> None:
    write(world.repo, {"tests/test_mul.py": FAILING_MUL, "src/mul.py": "x = 1\n"})
    (world.repo / ".gitignore").unlink()  # so bytecode shows as untracked
    (world.repo / "tests" / "__pycache__").mkdir()
    (world.repo / "tests" / "__pycache__" / "test_mul.cpython-312.pyc").write_bytes(b"\0")
    outcome = world.check()
    assert set(outcome.files) == {"tests/test_mul.py"}


def test_a_link_is_not_recorded_as_a_tester_file(world: World) -> None:
    write(world.repo, {"tests/test_mul.py": FAILING_MUL})
    try:
        (world.repo / "tests" / "test_alias.py").symlink_to(world.repo / "tests" / "test_mul.py")
    except OSError as err:
        pytest.skip(f"this machine can't create symlinks ({err})")
    found = tester_files(
        world.git,
        worktree=world.repo,
        run_folder=world.run_folder,
        base=world.base,
        tester=world.tester,
    )
    assert set(found) == {"tests/test_mul.py"}


def test_a_later_tester_attempt_keeps_earlier_files_that_still_exist(world: World) -> None:
    write(world.repo, {"tests/test_mul.py": FAILING_MUL, "tests/test_gone.py": FAILING_MUL})
    world.check(attempt=1)
    world.git.run(["add", "--all"], world.repo)
    world.git.run(["commit", "-q", "-m", "tester attempt 1"], world.repo)
    world.base = world.git.out(["rev-parse", "HEAD"], world.repo)
    (world.repo / "tests" / "test_gone.py").unlink()
    write(world.repo, {"tests/test_div.py": "def test_div():\n    from calc import div\n"})
    world.check(attempt=2)
    assert set(load_tester_files(world.run_folder)) == {"tests/test_div.py", "tests/test_mul.py"}


def test_a_timeout_fails_with_timeout(world: World) -> None:
    write(
        world.repo,
        {"tests/test_slow.py": "import time\n\n\ndef test_slow():\n    time.sleep(60)\n"},
    )
    outcome = world.check(timeout=1.0)
    assert outcome.kind == "timeout"
    assert outcome.reason.startswith("timeout")
    assert (world.run_folder / "test-output-tester-1.txt").is_file()


@pytest.mark.parametrize(("code", "kind"), [(1, "passed"), (0, "tests_pass")])
def test_other_runners_are_judged_by_the_exit_code(world: World, code: int, kind: str) -> None:
    write(world.repo, {"tests/check.py": f"raise SystemExit({code})\n"})
    outcome = world.check(f"{PYTHON} tests/check.py")
    assert outcome.kind == kind, outcome.reason


@pytest.mark.parametrize("command", [None, "  "])
def test_no_test_command_is_a_config_error(world: World, command: str | None) -> None:
    write(world.repo, {"tests/test_mul.py": FAILING_MUL})
    with pytest.raises(ConfigError, match=r"commands\.test"):
        world.check(command)


# ------------------------------------------------------------------ which file a test is in


@pytest.mark.parametrize(
    ("classname", "name", "rel", "expected"),
    [
        ("tests.test_mul", "test_mul", "tests/test_mul.py", True),
        ("tests.test_mul.TestMul", "test_one", "tests/test_mul.py", True),
        ("", "tests.test_mul", "tests/test_mul.py", True),  # a collection error
        ("tests.test_mul_more", "test_x", "tests/test_mul.py", False),
        ("tests.test_calc", "test_mul", "tests/test_mul.py", False),
        ("tests.test_mul", "test_mul", "app/tests/test_mul.py", True),  # rootdir is app/
        ("tests/sum.test.ts", "sum > adds", "tests/sum.test.ts", True),  # vitest
        ("tests/sum.test.ts", "sum > adds", "app/tests/sum.test.ts", True),
        ("tests/other.test.ts", "sum > adds", "tests/sum.test.ts", False),
    ],
)
def test_belongs(classname: str, name: str, rel: str, expected: bool) -> None:
    assert belongs(classname, name, rel) is expected
