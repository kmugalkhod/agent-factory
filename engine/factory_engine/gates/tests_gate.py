"""The tester gate (design: gates between stages, "Tests exist and fail before the build").

After the tester's stage the engine checks, in order:

1. The tester's diff adds or changes files it may write (its `write_paths`, judged by the same
   rules as the path hook). None → "no new tests", and the tests don't run.
2. The engine runs the repo's test command itself (`testrun`), with a clean environment and a
   timeout, and saves the output to the run folder. A timeout → "timeout".
3. For pytest and vitest it reads the JUnit report:
   - any collection, syntax, fixture or setup error, or no usable report → "broken tests";
   - no tests ran → "no tests ran";
   - none of the tests that ran is in the tester's files → "no new tests";
   - a test outside the tester's files fails → "existing tests fail";
   - none of the tester's tests fails → "tests pass before the build".
   Otherwise the gate passes: at least one new test fails on its assertions. A new test that
   passes next to failing ones is allowed (a check that existing behaviour is unchanged).
4. Other runners give the exit code only: 0 → "tests pass before the build", else pass.

The tester's files and their git blob ids go to `<run folder>/tester-files.json` for the test
lock (task 2.18, `gates/build.py`). The gate returns its decision; the pipeline logs `event()`
and retries the tester with `reason` on a fail.
"""

import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from factory_engine.config import RoleSettings
from factory_engine.errors import ConfigError, GateError, GitError
from factory_engine.events import EventLog, GateResult
from factory_engine.git_ops import Git, run_command
from factory_engine.providers import BASE_VARS
from factory_engine.safety.paths import PathPolicy, check_worktree_name
from factory_engine.testrun import (
    DEFAULT_TIMEOUT,
    CommandResult,
    JUnitCase,
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
BLOB_ID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")  # SHA-1 or SHA-256 repos
HASH_CHUNK = 100  # paths per `git hash-object` call, well inside Windows' command-line limit

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
    its git blob id (see `blob_ids`)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    files: dict[str, str]

    @field_validator("files")
    @classmethod
    def _check_entries(cls, files: dict[str, str]) -> dict[str, str]:
        for rel, blob in files.items():
            if not _safe_rel(rel):
                raise ValueError(f"{rel!r} is not a path relative to the worktree")
            if not BLOB_ID.fullmatch(blob):
                raise ValueError(f"{blob!r} (for {rel}) is not a git blob id")
        return files


def _safe_rel(rel: str) -> bool:
    """A plain worktree-relative path: `/`-separated, no drive, root, `.` or `..`."""
    windows = PureWindowsPath(rel)
    return not (
        not rel
        or "\\" in rel
        or rel.startswith("/")
        or windows.drive
        or windows.root
        or any(part in ("", ".", "..") for part in rel.split("/"))
    )


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
    files = tester_files(git, worktree=worktree, run_folder=run_folder, base=base, tester=tester)
    record_tester_files(git, run_folder, worktree, files)
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
    return judge(result, files)


# ------------------------------------------------------------------ the decision


def judge(result: CommandResult, files: Mapping[str, str]) -> TesterGateOutcome:
    """The gate's decision on one test run, given the tester's files. Pure."""

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
    new = [case for case in report.cases if _owned(case, files)]
    failing = tuple(case.id for case in new if case.outcome == "failed")
    passing = tuple(case.id for case in new if case.outcome == "passed")
    if not new:
        return outcome(
            "no_new_tests",
            "no new tests: none of the tests the command ran is in the tester's files "
            f"({_listed(sorted(files))}). Write the tests where the test runner collects them.",
        )
    others = [case.id for case in report.cases if case.outcome == "failed" and case not in new]
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


def belongs(classname: str, name: str, rel: str) -> bool:
    """Whether a JUnit test case comes from the file `rel` (worktree-relative, `/`).

    vitest names the file in `classname`. pytest gives a dotted module path (`tests.test_x`,
    then any classes), relative to its rootdir, which may be a subfolder of the worktree; for
    a file it couldn't collect, the module is the `name` and `classname` is empty."""
    owner = (classname or name).replace("\\", "/")
    rel = rel.replace("\\", "/")
    if "/" in owner:
        return owner == rel or rel.endswith("/" + owner) or owner.endswith("/" + rel)
    module = rel.removesuffix(".py").split("/")
    for start in range(len(module)):
        dotted = ".".join(module[start:])
        if owner == dotted or owner.startswith(dotted + "."):
            return True
    return False


def _owned(case: JUnitCase, files: Mapping[str, str]) -> bool:
    return any(belongs(case.classname, case.name, rel) for rel in files)


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
    git blob id of each. Deleted files, links (and files reached through one) and Python
    bytecode are left out: the lock covers regular files only."""
    status = git.run(["diff", "--name-status", "--no-renames", "-z", base, "--"], worktree).stdout
    fields = status.split("\0")
    changed = {
        fields[i + 1] for i in range(0, len(fields) - 1, 2) if fields[i] and fields[i] != "D"
    }
    untracked = git.run(["ls-files", "--others", "--exclude-standard", "-z"], worktree).stdout
    changed.update(name for name in untracked.split("\0") if name)
    policy = PathPolicy.build("tester", tester, worktree=worktree, run_folder=run_folder)
    found: list[str] = []
    for rel in sorted(changed):
        if is_bytecode(rel) or not regular_file(worktree, rel):
            continue
        if check_worktree_name(policy, rel).allowed:
            found.append(rel)
    return blob_ids(git, worktree, found)


def record_tester_files(
    git: Git, run_folder: Path, worktree: Path, files: Mapping[str, str]
) -> dict[str, str]:
    """Add `files` to `tester-files.json`. Files from an earlier tester stage that still exist
    stay, with their current blob id; those that are gone are dropped. Returns what was saved."""
    kept = [
        rel
        for rel in load_tester_files(run_folder)
        if rel not in files and regular_file(worktree, rel)
    ]
    merged = blob_ids(git, worktree, kept)
    merged.update(files)
    path = run_folder / TESTER_FILES
    try:
        saved = TesterFiles(files=dict(sorted(merged.items())))
    except ValidationError as err:
        raise GateError(
            f"{path}: can't record the tester's files ({err.error_count()} problems, for "
            f"example {err.errors()[0]['msg']}). Rename the file so its path is plain."
        ) from err
    write_atomic(path, saved.model_dump_json(indent=2) + "\n")
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


def blob_ids(git: Git, worktree: Path, rels: Sequence[str]) -> dict[str, str]:
    """The git blob id of each file (worktree-relative, `/`): its content as git would store
    it, from `git hash-object` with the filters each path's attributes and the repo's config
    choose, `core.autocrlf` among them. The tester gate records these and the builder gate
    compares them, always this one way.

    Why not the raw bytes: a checkout (a rebase, a worktree made again) may rewrite a file's
    line endings, which changes its bytes but never its blob id, so raw hashes would break the
    lock with no change made. Any change git would commit changes the blob id. A change of line
    endings alone, which git normalizes away, is not seen, and isn't committed either."""
    found: dict[str, str] = {}
    for start in range(0, len(rels), HASH_CHUNK):
        chunk = list(rels[start : start + HASH_CHUNK])
        try:
            out = git.run(["hash-object", "--", *chunk], worktree).stdout.split()
        except GitError as err:
            raise GateError(f"can't hash the tester's files in {worktree} ({err}).") from err
        if len(out) != len(chunk) or not all(BLOB_ID.fullmatch(blob) for blob in out):
            raise GateError(
                f"`git hash-object` in {worktree} gave {len(out)} ids for {len(chunk)} files."
            )
        found.update(zip(chunk, out, strict=True))
    return found


def through_link(worktree: Path, rel: str) -> bool:
    """Whether the entry `rel` (worktree-relative, `/`) is a link (a symlink or a junction) or
    lies in a folder reached through one. Judged by its own name: no link is followed. Parts
    that don't exist aren't links; one that can't be examined counts as a link."""
    path = worktree
    for part in rel.split("/"):
        path = path / part
        try:
            if path.is_symlink() or path.is_junction():
                return True
        except OSError:
            return True
    return False


def regular_file(worktree: Path, rel: str) -> bool:
    """Whether `rel` is a regular file reached without any link."""
    return not through_link(worktree, rel) and (worktree / rel).is_file()


def is_bytecode(rel: str) -> bool:
    path = PurePosixPath(rel)
    return "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo")
