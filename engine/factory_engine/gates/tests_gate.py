"""The tester gate (design: gates between stages, "Tests exist and fail before the build").

After the tester's stage the engine checks, in order:

1. The tester's diff adds or changes files it may write (its `write_paths`, judged by the same
   rules as the path hook). None → "no new tests", and the tests don't run.
2. The engine runs the repo's test command itself (`testrun`), with a clean environment and a
   timeout, and saves the output to the run folder. A timeout → "timeout".
3. For pytest and vitest it reads the JUnit report:
   - any collection, syntax, fixture or setup error, or no usable report → "broken tests";
   - no tests ran → "no tests ran";
   - none of the tests that ran is in the tester's files (this stage's and those recorded by
     an earlier tester stage) → "no new tests";
   - a test outside the tester's files fails → "existing tests fail";
   - none of the tester's tests fails → "tests pass before the build".
   Otherwise the gate passes: at least one new test fails on its assertions. A new test that
   passes next to failing ones is allowed (a check that existing behaviour is unchanged).
4. Other runners give the exit code only: 0 → "tests pass before the build", else pass.

A test case's file is the one its report names (pytest's `file`, vitest's `classname`), relative
to the runner's root. The root is found once per report: the folder (the worktree, one inside
it, or one above it) under which every reported file exists. A file that could sit under more
than one such root counts as the tester's only if it is the tester's under each of them.

The tester's files and their sha256 hashes go to `<run folder>/tester-files.json` for the test
lock (task 2.18). The gate returns its decision; the pipeline logs `event()` and retries the
tester with `reason` on a fail.
"""

import hashlib
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePath, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from factory_engine.config import RoleSettings
from factory_engine.errors import ConfigError, GateError
from factory_engine.events import EventLog, GateResult
from factory_engine.git_ops import Git, run_command
from factory_engine.providers import BASE_VARS
from factory_engine.safety.paths import PathPolicy, check_file_tool
from factory_engine.testrun import (
    DEFAULT_TIMEOUT,
    CommandResult,
    JUnitCase,
    JUnitReport,
    Runner,
    run_repo_command,
    write_atomic,
)

GATE = "tests"
TESTER_FILES = "tester-files.json"
MAX_LISTED = 10
# pytest's exit codes for an interrupted run, an internal error and a usage error, and for a
# run that collected no tests.
PYTEST_BROKEN = frozenset({2, 3, 4})
PYTEST_NO_TESTS = 5

Kind = Literal[
    "passed",
    "no_new_tests",
    "tests_pass",
    "broken_tests",
    "no_tests_ran",
    "other_tests_fail",
    "timeout",
]


class TesterFiles(BaseModel):
    """`tester-files.json`: each file the tester wrote (worktree-relative, `/`-separated) and
    the sha256 of its bytes."""

    model_config = ConfigDict(extra="forbid", strict=True)

    files: dict[str, str]


@dataclass(frozen=True)
class TesterGateOutcome:
    passed: bool
    kind: Kind
    reason: str
    files: Mapping[str, str] = field(default_factory=dict)  # this stage's files and hashes
    run: CommandResult | None = None  # None when the tests didn't run
    failing_new: tuple[str, ...] = ()  # test IDs in the tester's files that fail
    passing_new: tuple[str, ...] = ()  # and those that pass

    def event(self) -> GateResult:
        return GateResult(gate=GATE, passed=self.passed, reason=self.reason)


def check_tests(
    git: Git,
    *,
    worktree: Path,
    run_folder: Path,
    base: str,
    tester: RoleSettings,
    command: str | None,
    attempt: int,
    events: EventLog,
    parent_env: Mapping[str, str],
    timeout: float = DEFAULT_TIMEOUT,
    allow: Iterable[str] = BASE_VARS,
    runner: Runner = run_command,
    timer: Callable[[], float] = time.monotonic,
) -> TesterGateOutcome:
    """Judge the tester's stage. `base` is the commit the stage started on, `tester` the
    tester's merged settings, `command` the repo's `commands.test` and `parent_env` the
    engine's environment (pass `os.environ`; the command gets only what `allow` names, never
    credentials). Raises ConfigError when there is no usable test command."""
    if command is None or not command.strip():
        raise ConfigError(
            "the repo has no test command, so the tester's tests can't be checked. Set "
            "`commands.test` in factory.yaml (for example `uv run pytest`)."
        )
    worktree, run_folder = worktree.resolve(), run_folder.resolve()
    files = tester_files(git, worktree=worktree, run_folder=run_folder, base=base, tester=tester)
    # Judge by every file a tester stage wrote: a retry or rerun starting from a commit that
    # already holds earlier tester files must not count their failing tests as someone else's.
    recorded = record_tester_files(run_folder, worktree, files)
    if not files:
        allowed = ", ".join(tester.write_paths) or "nothing"
        return TesterGateOutcome(
            passed=False,
            kind="no_new_tests",
            reason=(
                f"no new tests: the tester's diff adds or changes no file it may write "
                f"({allowed}). Write tests for the plan's acceptance criteria there; they must "
                "fail until the feature is built."
            ),
        )
    result = run_repo_command(
        command,
        worktree=worktree,
        run_folder=run_folder,
        stage="tester",
        attempt=attempt,
        parent_env=parent_env,
        events=events,
        timeout=timeout,
        allow=allow,
        runner=runner,
        timer=timer,
    )
    known = git.run(
        ["ls-files", "--cached", "--others", "--exclude-standard", "-z"], worktree
    ).stdout
    index = FileIndex([name for name in known.split("\0") if name], worktree)
    return judge(result, recorded, index)


# ------------------------------------------------------------------ the decision


def judge(result: CommandResult, files: Mapping[str, str], index: "FileIndex") -> TesterGateOutcome:
    """The gate's decision on one test run, given the tester's files and an index of the
    worktree's files. Pure."""

    def outcome(kind: Kind, reason: str, **found: tuple[str, ...]) -> TesterGateOutcome:
        if kind != "passed":
            reason += f" The test output is in {result.output_path}."
        return TesterGateOutcome(
            passed=kind == "passed", kind=kind, reason=reason, files=files, run=result, **found
        )

    if result.timed_out:
        return outcome(
            "timeout",
            f"timeout: the test command ran longer than {result.timeout:g}s and was stopped. "
            "Find the test that hangs or waits (a loop, a sleep, input, the network) and make "
            "it fail fast instead.",
        )
    code = result.exit_code
    report = result.report
    if result.runner is None:
        if code == 0:
            return outcome(
                "tests_pass",
                "tests pass before the build: the test command succeeded (exit 0), but the new "
                "tests must fail until the feature is built.",
            )
        return outcome(
            "passed",
            f"the test command fails before the build (exit {code}). It writes no JUnit report, "
            "so the gate checked the exit code only.",
        )

    if report is None or report.errors or (result.runner == "pytest" and code in PYTEST_BROKEN):
        return outcome("broken_tests", _broken(result))
    if not report.cases or (result.runner == "pytest" and code == PYTEST_NO_TESTS):
        return outcome(
            "no_tests_ran",
            f"no tests ran: the test command (exit {code}) collected no tests. Check that the "
            "new test files are named and placed so the test runner finds them.",
        )
    owned = owned_cases(report, result.runner, files, index)
    new = [case for case, mine in zip(report.cases, owned, strict=True) if mine]
    failing = tuple(case.id for case in new if case.outcome == "failed")
    passing = tuple(case.id for case in new if case.outcome == "passed")
    if not new:
        return outcome(
            "no_new_tests",
            "no new tests: none of the tests the command ran is in the tester's files "
            f"({_listed(sorted(files))}). Write the tests where the test runner collects them.",
        )
    others = [
        case.id
        for case, mine in zip(report.cases, owned, strict=True)
        if case.outcome == "failed" and not mine
    ]
    if others:
        return outcome(
            "other_tests_fail",
            f"existing tests fail: {_listed(others)}. Before the build only the tester's new "
            "tests may fail; tests that already exist must still pass.",
        )
    if not failing or code == 0:
        listed = _listed(passing) if passing else "none of them fails"
        return outcome(
            "tests_pass",
            f"tests pass before the build: {listed}. The new tests must fail until the "
            "feature is built; make them check the behaviour the plan asks for.",
            passing_new=passing,
        )
    return outcome(
        "passed",
        f"{len(failing)} new {'test fails' if len(failing) == 1 else 'tests fail'} before the "
        f"build, as it should: {_listed(failing)}.",
        failing_new=failing,
        passing_new=passing,
    )


def _broken(result: CommandResult) -> str:
    """The reason for broken tests. Never says "tests fail": the tests didn't get that far."""
    report = result.report
    if report is None:
        detail = result.report_problem or "no report"
        found = f"the test command (exit {result.exit_code}) left no usable report ({detail})"
    else:
        errored = [
            f"{case.id} ({case.message})" if case.message else case.id
            for case in report.cases
            if case.outcome == "error"
        ]
        what = _listed(errored) if errored else f"{report.errors} errors"
        found = f"collection, syntax or setup errors: {what}"
        if not errored and result.exit_code is not None:
            found += f" (exit {result.exit_code})"
    return (
        f"broken tests: {found}. Fix them so every test runs and fails on its own "
        "assertions. A missing feature must fail inside a test: import the code under test "
        "inside the test function, not at the top of the file, and use only fixtures that exist."
    )


class FileIndex:
    """The worktree's files (tracked and untracked, not ignored), for finding which file a
    test runner's path names."""

    def __init__(
        self, files: Iterable[str], worktree: PurePath, *, windows: bool = sys.platform == "win32"
    ) -> None:
        self.windows = windows
        self.worktree_parts = tuple(self.key(part) for part in worktree.parts)
        self.files = {self.key(rel): rel for rel in files}
        self.by_name: dict[str, list[str]] = {}
        for rel in self.files.values():
            self.by_name.setdefault(self.key(rel.rsplit("/", 1)[-1]), []).append(rel)

    def key(self, text: str) -> str:
        return text.casefold() if self.windows else text

    def roots(self, path: str) -> dict[str, str]:
        """Each place the runner's root could be such that `path` (relative to that root)
        names a worktree file, mapped to the file (worktree-relative). A root inside the
        worktree is its worktree-relative folder ("" for the worktree itself); one above it is
        `../` per level."""
        parts = [part for part in path.replace("\\", "/").split("/") if part not in ("", ".")]
        if not parts or path.startswith(("/", "\\")) or ".." in parts or ":" in parts[0]:
            return {}
        joined = self.key("/".join(parts))
        found: dict[str, str] = {}
        for rel in self.by_name.get(self.key(parts[-1]), []):
            key = self.key(rel)
            if key == joined:
                found[""] = rel
            elif key.endswith("/" + joined):
                found[rel[: len(rel) - len(joined) - 1]] = rel
        for up in range(1, len(self.worktree_parts)):
            tail = self.worktree_parts[-up:]
            if len(parts) > up and tuple(self.key(p) for p in parts[:up]) == tail:
                rel = self.files.get(self.key("/".join(parts[up:])))
                if rel is not None:
                    found["../" * up] = rel
        return found


def case_path(case: JUnitCase, runner: str | None) -> str:
    """The file a test case's report names, relative to the runner's root: pytest's `file`
    (in the `xunit1` report the engine asks for), vitest's `classname`."""
    return case.file if runner == "pytest" else case.classname


def owned_cases(
    report: JUnitReport, runner: str | None, files: Mapping[str, str], index: FileIndex
) -> list[bool]:
    """For each test case, whether it is in one of the tester's `files`. The runner's root is
    the one under which every reported file exists; when more than one fits, a case is the
    tester's only if it is under each of them. A case whose file can't be found isn't."""
    candidates = [index.roots(case_path(case, runner)) for case in report.cases]
    found = [set(roots) for roots in candidates if roots]
    shared = set.intersection(*found) if found else set()
    mine = {index.key(rel) for rel in files}
    return [
        bool(roots) and bool(shared) and all(index.key(roots[root]) in mine for root in shared)
        for roots in candidates
    ]


def _listed(items: Sequence[str]) -> str:
    shown = "; ".join(items[:MAX_LISTED])
    more = len(items) - MAX_LISTED
    return f"{shown}; and {more} more" if more > 0 else shown


# ------------------------------------------------------------------ the tester's files


def tester_files(
    git: Git, *, worktree: Path, run_folder: Path, base: str, tester: RoleSettings
) -> dict[str, str]:
    """The files the tester's stage added or changed since `base` (committed or not,
    untracked included, ignored ones left out) that its settings let it write, with the
    sha256 of each. Deleted files and Python bytecode are left out."""
    status = git.run(["diff", "--name-status", "--no-renames", "-z", base, "--"], worktree).stdout
    fields = status.split("\0")
    changed = {
        fields[i + 1] for i in range(0, len(fields) - 1, 2) if fields[i] and fields[i] != "D"
    }
    untracked = git.run(["ls-files", "--others", "--exclude-standard", "-z"], worktree).stdout
    changed.update(name for name in untracked.split("\0") if name)
    policy = PathPolicy.build("tester", tester, worktree=worktree, run_folder=run_folder)
    found: dict[str, str] = {}
    for rel in sorted(changed):
        path = worktree / rel
        if _bytecode(rel) or not path.is_file():
            continue
        if check_file_tool(policy, "Write", {"file_path": str(policy.worktree / rel)}).allowed:
            found[rel] = _sha256(path)
    return found


def record_tester_files(
    run_folder: Path, worktree: Path, files: Mapping[str, str]
) -> dict[str, str]:
    """Add `files` to `tester-files.json`. Files from an earlier tester stage that still exist
    stay, with their current hash; those that are gone are dropped. Returns what was saved."""
    merged = {
        rel: _sha256(worktree / rel)
        for rel in load_tester_files(run_folder)
        if rel not in files and (worktree / rel).is_file()
    }
    merged.update(files)
    saved = TesterFiles(files=dict(sorted(merged.items())))
    write_atomic(run_folder / TESTER_FILES, saved.model_dump_json(indent=2) + "\n")
    return saved.files


def load_tester_files(run_folder: Path) -> dict[str, str]:
    """The tester's files and hashes from `tester-files.json`; empty when there is none yet."""
    path = run_folder / TESTER_FILES
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as err:
        raise GateError(f"{path}: can't read the tester's files ({err}).") from err
    try:
        return TesterFiles.model_validate_json(text).files
    except ValidationError as err:
        raise GateError(
            f"{path}: invalid ({err.error_count()} problems). It records the tester's files "
            "for the test lock; rerun the tester stage to rebuild it."
        ) from err


def _bytecode(rel: str) -> bool:
    path = PurePosixPath(rel)
    return "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo")


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as err:
        raise GateError(f"{path}: can't read the tester's file to hash it ({err}).") from err
