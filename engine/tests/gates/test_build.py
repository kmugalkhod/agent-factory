"""Tests for the builder gate and the test lock: real pytest runs on small fixture repos made in
`tmp_path`, as for the tester gate."""

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
from factory_engine.gates.build import GATE, BuildGateOutcome, check_build, tail
from factory_engine.gates.tests_gate import TESTER_FILES, record_tester_files, tester_files
from factory_engine.git_ops import Git

PYTHON = shlex.quote(Path(sys.executable).as_posix())
PYTEST = f"{PYTHON} -m pytest"
LOCKED = "tests/test_mul.py"

BASE_FILES = {
    "pytest.ini": "[pytest]\npythonpath = src\n",
    ".gitignore": "__pycache__/\n.pytest_cache/\n",
    "src/calc.py": "def add(a, b):\n    return a + b\n",
    "tests/test_calc.py": "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
}
# The tester's test: fails until `mul` is built.
TESTER_TEST = "def test_mul():\n    from calc import mul\n\n    assert mul(2, 3) == 6\n"
ADD = BASE_FILES["src/calc.py"]
GOOD_BUILD = ADD + "\n\ndef mul(a, b):\n    return a * b\n"
WRONG_BUILD = ADD + "\n\ndef mul(a, b):\n    return a + b\n"
SLOW_BUILD = "import time\n\n" + ADD + "\n\ndef mul(a, b):\n    time.sleep(60)\n    return a * b\n"
# `test_calc.py` imports this at the top, so a syntax error stops its collection.
BROKEN_BUILD = "def add(a, b):\n    return a +\n"
# Skips every test that imports the code: a way to weaken tests without touching them.
SKIPPING_BUILD = "import pytest\n\npytest.skip('not today', allow_module_level=True)\n"


def clock() -> datetime:
    return datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def write(root: Path, files: dict[str, str]) -> None:
    """Write each file's bytes exactly (`\\n` stays LF on every platform)."""
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))


class World:
    """A fixture repo with a base commit, a run folder and, after `tester_stage`, the tester's
    committed tests recorded in `tester-files.json`, as the tester gate leaves them."""

    def __init__(self, tmp_path: Path, git_env: dict[str, str], *, autocrlf: bool = False) -> None:
        self.git = Git(git_env)
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        self.run_folder = tmp_path / "run"
        self.run_folder.mkdir()
        write(self.repo, BASE_FILES)
        self.git.run(["init", "-q", "-b", "main"], self.repo)
        self.git.run(["config", "core.autocrlf", "true" if autocrlf else "false"], self.repo)
        self.commit("base")
        self.base = self.head()
        self.log = EventLog(self.run_folder, repo="demo", run_id=1, clock=clock)
        settings = default_settings(sys.platform)
        self.tester: RoleSettings = settings.roles.tester
        self.builder: RoleSettings = settings.roles.builder

    def head(self) -> str:
        return self.git.out(["rev-parse", "HEAD"], self.repo)

    def commit(self, message: str) -> None:
        self.git.run(["add", "--all"], self.repo)
        self.git.run(["commit", "-q", "-m", message], self.repo)

    def tester_stage(self, files: dict[str, str]) -> None:
        start = self.head()
        write(self.repo, files)
        found = tester_files(
            self.git, worktree=self.repo, run_folder=self.run_folder, base=start, tester=self.tester
        )
        record_tester_files(self.git, self.run_folder, self.repo, found)
        self.commit("tester")
        self.base = self.head()  # the builder's stage starts here

    def check(
        self,
        command: str | None = PYTEST,
        *,
        attempt: int = 1,
        timeout: float = 120.0,
        tester_stage: bool = True,
    ) -> BuildGateOutcome:
        return check_build(
            self.git,
            worktree=self.repo,
            run_folder=self.run_folder,
            base=self.base,
            builder=self.builder,
            command=command,
            attempt=attempt,
            events=self.log,
            parent_env=dict(os.environ),
            tester_stage=tester_stage,
            timeout=timeout,
        )


@pytest.fixture
def world(tmp_path: Path, git_env: dict[str, str]) -> World:
    made = World(tmp_path, git_env)
    made.tester_stage({LOCKED: TESTER_TEST})
    return made


def symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except OSError as err:
        pytest.skip(f"this machine can't create symlinks ({err})")


# ------------------------------------------------------------------ the test run


def test_passing_tests_and_a_clean_diff_pass(world: World) -> None:
    write(world.repo, {"src/calc.py": GOOD_BUILD})
    outcome = world.check()
    assert outcome.passed, outcome.reason
    assert outcome.kind == "passed"
    assert not outcome.flagged
    assert outcome.run is not None
    assert (world.run_folder / "test-output-builder-1.txt").is_file()


def test_a_test_run_event_is_logged_and_the_gate_result_is_build(world: World) -> None:
    write(world.repo, {"src/calc.py": GOOD_BUILD})
    outcome = world.check()
    [event] = read_events(world.run_folder)
    assert isinstance(event.body, ev.TestRun)
    assert event.role == "builder"
    assert (event.body.passed, event.body.failed) == (2, 0)
    result = outcome.event()
    assert isinstance(result, ev.GateResult)
    assert (result.gate, result.passed, result.reason) == (GATE, True, outcome.reason)


def test_failing_tests_fail_with_the_output_attached(world: World) -> None:
    write(world.repo, {"src/calc.py": WRONG_BUILD})
    outcome = world.check(attempt=2)
    assert (outcome.passed, outcome.kind, outcome.flagged) == (False, "tests_fail", False)
    assert outcome.reason.startswith("tests fail")
    assert "tests.test_mul::test_mul" in outcome.reason
    output = world.run_folder / "test-output-builder-2.txt"
    assert str(output) in outcome.reason
    assert "test_mul" in outcome.output_tail
    message = outcome.resume_message()
    assert outcome.reason in message
    assert outcome.output_tail in message


def test_a_timeout_fails_with_timeout(world: World) -> None:
    write(world.repo, {"src/calc.py": SLOW_BUILD})
    outcome = world.check(timeout=1.0)
    assert (outcome.kind, outcome.flagged) == ("timeout", False)
    assert outcome.reason.startswith("timeout")
    assert (world.run_folder / "test-output-builder-1.txt").is_file()


def test_tests_the_code_breaks_at_collection_fail_as_broken_tests(world: World) -> None:
    write(world.repo, {"src/calc.py": BROKEN_BUILD})
    outcome = world.check()
    assert (outcome.kind, outcome.flagged) == ("broken_tests", False)
    assert outcome.reason.startswith("broken tests")


def test_skipped_tester_tests_fail(world: World) -> None:
    write(world.repo, {"src/calc.py": SKIPPING_BUILD})
    outcome = world.check()
    assert outcome.kind == "tests_skipped", outcome.reason
    assert "tests.test_mul::test_mul" in outcome.reason


@pytest.mark.parametrize(("code", "kind"), [(0, "passed"), (1, "tests_fail")])
def test_other_runners_are_judged_by_the_exit_code(world: World, code: int, kind: str) -> None:
    write(world.repo, {"src/calc.py": GOOD_BUILD, "src/check.py": f"raise SystemExit({code})\n"})
    outcome = world.check(f"{PYTHON} src/check.py")
    assert outcome.kind == kind, outcome.reason


@pytest.mark.parametrize("command", [None, "  "])
def test_no_test_command_is_a_config_error(world: World, command: str | None) -> None:
    write(world.repo, {"src/calc.py": GOOD_BUILD})
    with pytest.raises(ConfigError, match=r"commands\.test"):
        world.check(command)


# ------------------------------------------------------------------ the test lock


@pytest.mark.parametrize("change", ["changed", "deleted"])
def test_a_changed_or_deleted_test_file_fails_and_is_flagged(world: World, change: str) -> None:
    write(world.repo, {"src/calc.py": GOOD_BUILD})
    locked = world.repo / LOCKED
    if change == "changed":  # weakened: still passes, no longer checks anything
        locked.write_bytes(b"def test_mul():\n    assert True\n")
    else:
        locked.unlink()
    outcome = world.check()
    assert (outcome.passed, outcome.kind, outcome.flagged) == (False, "test_lock", True)
    assert outcome.reason.startswith("test lock")
    assert LOCKED in outcome.reason
    expected = ((LOCKED,), ()) if change == "changed" else ((), (LOCKED,))
    assert (outcome.changed_tests, outcome.deleted_tests) == expected
    assert outcome.run is None  # the tests don't run
    assert read_events(world.run_folder) == []


def test_a_test_file_replaced_by_a_link_fails_the_lock(world: World) -> None:
    write(world.repo, {"src/calc.py": GOOD_BUILD, "src/copy.py": TESTER_TEST})
    locked = world.repo / LOCKED
    locked.unlink()
    symlink_or_skip(locked, world.repo / "src" / "copy.py")
    outcome = world.check()
    assert outcome.kind == "test_lock", outcome.reason
    assert outcome.changed_tests == (LOCKED,)


def test_the_lock_holds_when_a_checkout_rewrites_line_endings(
    tmp_path: Path, git_env: dict[str, str]
) -> None:
    """A rebase or a fresh checkout with `core.autocrlf` rewrites LF as CRLF; that's no change."""
    world = World(tmp_path, git_env, autocrlf=True)
    world.tester_stage({LOCKED: TESTER_TEST})
    locked = world.repo / LOCKED
    assert b"\r\n" not in locked.read_bytes()
    locked.unlink()
    world.git.run(["checkout", "--", LOCKED], world.repo)
    assert b"\r\n" in locked.read_bytes()  # other bytes than the tester wrote, same blob
    write(world.repo, {"src/calc.py": GOOD_BUILD})
    outcome = world.check()
    assert outcome.passed, outcome.reason


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(None, id="missing"),
        pytest.param("not json", id="not-json"),
        pytest.param('{"files": []}', id="wrong-shape"),
        pytest.param('{"files": {}}', id="no-files"),
        pytest.param('{"files": {"tests/test_mul.py": "abc"}}', id="not-a-blob-id"),
        pytest.param('{"files": {"../outside.py": "' + "0" * 40 + '"}}', id="outside-path"),
        pytest.param('{"files": {"/abs/test.py": "' + "0" * 40 + '"}}', id="absolute-path"),
    ],
)
def test_a_missing_or_malformed_tester_files_record_fails_closed(
    world: World, content: str | None
) -> None:
    write(world.repo, {"src/calc.py": GOOD_BUILD})
    record = world.run_folder / TESTER_FILES
    if content is None:
        record.unlink()
    else:
        record.write_text(content, encoding="utf-8")
    outcome = world.check()
    assert (outcome.passed, outcome.kind, outcome.flagged) == (False, "test_lock", True)
    assert outcome.reason.startswith("test lock")
    assert outcome.run is None


def test_without_a_tester_stage_there_is_nothing_to_lock(
    tmp_path: Path, git_env: dict[str, str]
) -> None:
    world = World(tmp_path, git_env)
    write(world.repo, {"src/calc.py": GOOD_BUILD})
    outcome = world.check(tester_stage=False)
    assert outcome.passed, outcome.reason


# ------------------------------------------------------------------ the builder's paths


@pytest.mark.parametrize(
    "change",
    [
        pytest.param("added", id="writes-outside"),
        pytest.param("deleted", id="deletes-outside"),
        pytest.param("new-test", id="adds-a-test"),
    ],
)
def test_a_diff_outside_the_builder_paths_fails_and_is_flagged(world: World, change: str) -> None:
    write(world.repo, {"src/calc.py": GOOD_BUILD})
    if change == "added":
        write(world.repo, {"notes.txt": "hi\n"})
        name = "notes.txt"
    elif change == "deleted":
        (world.repo / "pytest.ini").unlink()
        name = "pytest.ini"
    else:
        write(world.repo, {"tests/test_extra.py": "def test_extra():\n    pass\n"})
        name = "tests/test_extra.py"
    outcome = world.check()
    assert (outcome.passed, outcome.kind, outcome.flagged) == (False, "outside_paths", True)
    assert name in outcome.reason
    assert outcome.outside and outcome.outside[0].startswith(name)
    assert outcome.run is None


def test_a_link_in_the_builder_diff_fails(world: World) -> None:
    write(world.repo, {"src/calc.py": GOOD_BUILD})
    symlink_or_skip(world.repo / "src" / "alias.py", world.repo / "src" / "calc.py")
    outcome = world.check()
    assert outcome.kind == "outside_paths", outcome.reason
    assert "src/alias.py" in outcome.reason


# ------------------------------------------------------------------ the resume message


def test_tail_keeps_the_last_lines() -> None:
    text = "\n".join(f"line {i}" for i in range(200)) + "\n"
    assert tail(text, lines=3) == "line 197\nline 198\nline 199"


def test_tail_starts_at_a_whole_line_when_cut_by_length() -> None:
    assert tail("a" * 10 + "\n" + "b" * 10, lines=5, chars=12) == "b" * 10


def test_the_resume_message_fences_the_output_safely() -> None:
    outcome = BuildGateOutcome(
        passed=False, kind="tests_fail", reason="tests fail: x.", output_tail="```\nboom"
    )
    assert "````\n```\nboom\n````" in outcome.resume_message()


def test_a_flagged_fail_has_no_output_to_resume_with() -> None:
    outcome = BuildGateOutcome(passed=False, kind="test_lock", reason="test lock: x.")
    assert outcome.flagged
    assert outcome.resume_message() == "test lock: x."
