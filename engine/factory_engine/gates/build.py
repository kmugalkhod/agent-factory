"""The builder gate and the test lock (design: gates between stages, "Engine runs the test
command itself; diff touches only allowed paths"; the tester's tests are locked from the builder).

After the builder's stage the engine checks, in order:

1. The test lock. Every file in `<run folder>/tester-files.json` (written by the tester gate)
   must still be a regular file with the blob id the tester left (`tests_gate.blob_ids`, the
   same method). A changed, deleted or linked file fails, even when the path hook missed how
   it happened. When the run had a tester stage, a missing, unreadable or invalid record fails
   too: without it nothing can be checked.
2. The builder's diff (since the commit its stage started on: added, changed and deleted files,
   untracked included, ignored ones and Python bytecode left out) touches only paths the
   builder may write, each judged by its own name with the path hook's rules. A changed entry
   that is a link, or lies through one, fails: links are never followed.
   Both are rules the hooks enforce, so a fail there is flagged: the pipeline stops the run for
   you instead of resuming the builder. The tests don't run.
3. The engine runs the repo's test command (`testrun`), with a clean environment and a timeout,
   and saves the output to the run folder. Python reads no bytecode from the worktree, so a
   planted `.pyc` can't stand in for a locked test.
4. For pytest and vitest it reads the JUnit report: a timeout → "timeout"; any collection,
   syntax or setup error, or no usable report → "broken tests"; no tests → "no tests ran";
   a failing test → "tests fail"; none of the tester's tests ran → "no tests ran"; one of them
   skipped → "tests skipped" (the builder never skips a test). Other runners give the exit
   code only. Otherwise the gate passes.

The gate returns its decision; the pipeline logs `event()` and, on a fail that isn't flagged,
resumes the builder with `resume_message()`: the reason and the end of the test output.
"""

import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from factory_engine.config import RoleSettings
from factory_engine.errors import ConfigError, GateError
from factory_engine.events import EventLog, GateResult
from factory_engine.gates.tests_gate import (
    TESTER_FILES,
    belongs,
    blob_ids,
    is_bytecode,
    load_tester_files,
    through_link,
)
from factory_engine.git_ops import Git, run_command
from factory_engine.providers import BASE_VARS
from factory_engine.safety.paths import PathPolicy, check_worktree_name
from factory_engine.testrun import (
    DEFAULT_TIMEOUT,
    CommandResult,
    JUnitCase,
    Runner,
    run_repo_command,
)

GATE = "build"
MAX_LISTED = 10
TAIL_LINES = 60
TAIL_CHARS = 6000
# pytest's exit codes for an interrupted run, an internal error and a usage error, and for a
# run that collected no tests.
PYTEST_BROKEN = frozenset({2, 3, 4})
PYTEST_NO_TESTS = 5

Kind = Literal[
    "passed",
    "test_lock",
    "outside_paths",
    "timeout",
    "broken_tests",
    "no_tests_ran",
    "tests_fail",
    "tests_skipped",
]
FLAGGED: frozenset[Kind] = frozenset({"test_lock", "outside_paths"})
_STOPS = (
    " The hooks should have stopped this, so the run stops for you: check how it happened "
    "before deciding the run."
)


@dataclass(frozen=True)
class BuildGateOutcome:
    passed: bool
    kind: Kind
    reason: str
    locked: Mapping[str, str] = field(default_factory=dict)  # the tester's files and blob ids
    changed_tests: tuple[str, ...] = ()  # locked files changed (or replaced by a link)
    deleted_tests: tuple[str, ...] = ()  # locked files gone
    outside: tuple[str, ...] = ()  # "path (why)" for each path the builder may not touch
    run: CommandResult | None = None  # None when the tests didn't run
    output_tail: str = ""  # the end of the test output, on a fail

    @property
    def flagged(self) -> bool:
        """The builder broke a rule the hooks enforce: stop the run, don't resume it."""
        return self.kind in FLAGGED

    def event(self) -> GateResult:
        return GateResult(gate=GATE, passed=self.passed, reason=self.reason)

    def resume_message(self) -> str:
        """What the builder is resumed with on a fail: the reason and the end of the output."""
        if not self.output_tail:
            return self.reason
        longest = max((len(run) for run in re.findall(r"`+", self.output_tail)), default=0)
        fence = "`" * max(3, longest + 1)
        return (
            f"{self.reason}\n\nThe end of the test output:\n\n"
            f"{fence}\n{self.output_tail}\n{fence}\n"
        )


def check_build(
    git: Git,
    *,
    worktree: Path,
    run_folder: Path,
    base: str,
    builder: RoleSettings,
    command: str | None,
    attempt: int,
    events: EventLog,
    parent_env: Mapping[str, str],
    tester_stage: bool = True,
    timeout: float = DEFAULT_TIMEOUT,
    allow: Iterable[str] = BASE_VARS,
    runner: Runner = run_command,
    timer: Callable[[], float] = time.monotonic,
) -> BuildGateOutcome:
    """Judge the builder's stage. `base` is the commit the stage started on, `builder` the
    builder's merged settings, `command` the repo's `commands.test` and `parent_env` the
    engine's environment (pass `os.environ`; the command gets only what `allow` names).
    `tester_stage` is False only for a run with no tester stage (bootstrap); otherwise
    `tester-files.json` must be there. Raises ConfigError when there is no usable test
    command; a lock or path check that can't run fails the gate (fail closed)."""
    if command is None or not command.strip():
        raise ConfigError(
            "the repo has no test command, so the build can't be checked. Set `commands.test` "
            "in factory.yaml (for example `uv run pytest`)."
        )
    try:
        locked = _locked(run_folder, tester_stage)
        changed, deleted = check_lock(git, worktree, locked)
    except Exception as err:  # fail closed
        return BuildGateOutcome(
            passed=False,
            kind="test_lock",
            reason=f"test lock: the tester's files can't be checked ({err})." + _STOPS,
        )
    try:
        outside = outside_paths(
            git, worktree=worktree, run_folder=run_folder, base=base, builder=builder, skip=locked
        )
    except Exception as err:  # fail closed
        return BuildGateOutcome(
            passed=False,
            kind="outside_paths",
            reason=f"outside allowed paths: the builder's diff can't be checked ({err})." + _STOPS,
            locked=locked,
        )
    if changed or deleted or outside:
        return _flagged(locked, changed, deleted, outside, builder)

    result = run_repo_command(
        command,
        worktree=worktree,
        run_folder=run_folder,
        stage="builder",
        attempt=attempt,
        parent_env=parent_env,
        events=events,
        timeout=timeout,
        allow=allow,
        # Python reads bytecode from here (empty: none is written) and never from the
        # worktree's `__pycache__`, where code run during the stage could plant some.
        extra={"PYTHONPYCACHEPREFIX": str(run_folder.resolve() / "cache" / "pycache")},
        runner=runner,
        timer=timer,
    )
    return judge(result, locked)


# ------------------------------------------------------------------ the test lock


def _locked(run_folder: Path, tester_stage: bool) -> dict[str, str]:
    path = run_folder / TESTER_FILES
    if tester_stage and not path.is_file():
        raise GateError(f"{path} is missing; the tester gate writes it")
    locked = load_tester_files(run_folder)
    if tester_stage and not locked:
        raise GateError(f"{path} lists no files, but the tester's stage passed with new tests")
    return locked


def check_lock(
    git: Git, worktree: Path, locked: Mapping[str, str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(changed, deleted): the locked files that differ from the tester's version, or are now
    a link or not a file, and those that are gone."""
    changed: list[str] = []
    deleted: list[str] = []
    present: list[str] = []
    for rel in sorted(locked):
        path = worktree / rel
        if through_link(worktree, rel):
            changed.append(rel)
        elif path.is_file():
            present.append(rel)
        elif path.exists(follow_symlinks=False):
            changed.append(rel)  # a folder, or something else that isn't a file
        else:
            deleted.append(rel)
    now = blob_ids(git, worktree, present)
    changed += [rel for rel in present if now[rel] != locked[rel]]
    return tuple(sorted(changed)), tuple(deleted)


# ------------------------------------------------------------------ the builder's paths


def outside_paths(
    git: Git,
    *,
    worktree: Path,
    run_folder: Path,
    base: str,
    builder: RoleSettings,
    skip: Iterable[str] = (),
) -> tuple[str, ...]:
    """ "path (why)" for each path the builder's diff touches that it may not write. Paths in
    `skip` (the locked files, judged by the lock) are left out."""
    policy = PathPolicy.build("builder", builder, worktree=worktree, run_folder=run_folder)
    skipped = set(skip)
    refused: list[str] = []
    for rel in changed_paths(git, worktree, base):
        if rel in skipped:
            continue
        if through_link(worktree, rel):
            refused.append(f"{rel} (a link, or inside one; the builder writes files, not links)")
            continue
        decision = check_worktree_name(policy, rel)
        if not decision.allowed:
            refused.append(f"{rel} ({decision.reason.removeprefix('Blocked: ')})")
    return tuple(refused)


def changed_paths(git: Git, worktree: Path, base: str) -> list[str]:
    """Every worktree-relative path added, changed or deleted since `base`, committed or not,
    untracked included; ignored files and Python bytecode left out."""
    tracked = git.run(["diff", "--name-only", "--no-renames", "-z", base, "--"], worktree).stdout
    names = {name for name in tracked.split("\0") if name}
    untracked = git.run(["ls-files", "--others", "--exclude-standard", "-z"], worktree).stdout
    names.update(name for name in untracked.split("\0") if name)
    return sorted(name for name in names if not is_bytecode(name))


def _flagged(
    locked: Mapping[str, str],
    changed: Sequence[str],
    deleted: Sequence[str],
    outside: Sequence[str],
    builder: RoleSettings,
) -> BuildGateOutcome:
    found: list[str] = []
    if changed or deleted:
        what = [f"changed {_listed(changed)}"] if changed else []
        what += [f"deleted {_listed(deleted)}"] if deleted else []
        found.append(
            f"test lock: the builder's stage {' and '.join(what)}, written by the tester and "
            "locked from the builder. Tests are never weakened, skipped or deleted to pass."
        )
    if outside:
        allowed = ", ".join(builder.write_paths) or "nothing"
        found.append(
            f"outside allowed paths: the builder's diff touches paths it may not change: "
            f"{_listed(outside)}. The builder may write only {allowed}."
        )
    return BuildGateOutcome(
        passed=False,
        kind="test_lock" if changed or deleted else "outside_paths",
        reason=" ".join(found) + _STOPS,
        locked=locked,
        changed_tests=tuple(changed),
        deleted_tests=tuple(deleted),
        outside=tuple(outside),
    )


# ------------------------------------------------------------------ the decision


def judge(result: CommandResult, locked: Mapping[str, str]) -> BuildGateOutcome:
    """The gate's decision on one test run, given the tester's files. Pure."""

    def outcome(kind: Kind, reason: str) -> BuildGateOutcome:
        failed = kind != "passed"
        if failed:
            reason += f" The test output is in {result.output_path}."
        return BuildGateOutcome(
            passed=not failed,
            kind=kind,
            reason=reason,
            locked=locked,
            run=result,
            output_tail=tail(result.output) if failed else "",
        )

    if result.timed_out:
        return outcome(
            "timeout",
            f"timeout: the test command ran longer than {result.timeout:g}s and was stopped. "
            "Find what hangs or waits (a loop, a sleep, input, the network) in the code.",
        )
    code = result.exit_code
    report = result.report
    if result.runner is None:
        if code == 0:
            return outcome(
                "passed",
                "the test command passes (exit 0). It writes no JUnit report, so the gate "
                "checked the exit code only.",
            )
        return outcome("tests_fail", f"tests fail: the test command exits {code}.")

    is_pytest = result.runner == "pytest"
    if report is None or report.errors or (is_pytest and code in PYTEST_BROKEN):
        return outcome("broken_tests", _broken(result))
    if not report.cases or (is_pytest and code == PYTEST_NO_TESTS):
        return outcome(
            "no_tests_ran",
            f"no tests ran: the test command (exit {code}) collected no tests. The tests are "
            "locked; check that the code doesn't stop them from being found.",
        )
    failing = [case.id for case in report.cases if case.outcome == "failed"]
    if failing or code != 0:
        listed = _listed(failing) if failing else f"the test command exits {code}"
        return outcome(
            "tests_fail",
            f"tests fail: {listed}. Change the code until they pass; the tests are locked.",
        )
    tester = [case for case in report.cases if _owned(case, locked)]
    if locked and not tester:
        return outcome(
            "no_tests_ran",
            f"no tests ran from the tester's files ({_listed(sorted(locked))}). They must run "
            "and pass; check that the code doesn't stop them from being collected.",
        )
    skipped = [case.id for case in tester if case.outcome == "skipped"]
    if skipped:
        return outcome(
            "tests_skipped",
            f"tests skipped: {_listed(skipped)}, written by the tester. They must run and "
            "pass; never skip a test. If the tester's skip is right, say so in your handoff.",
        )
    return outcome(
        "passed",
        f"the tests pass: {report.passed} passed, {report.skipped} skipped, "
        f"{len(tester)} of them the tester's.",
    )


def _broken(result: CommandResult) -> str:
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
        found = "collection, syntax or setup errors: " + (
            _listed(errored) if errored else f"{report.errors} errors"
        )
    return (
        f"broken tests: {found}. The tests are locked, so fix the code they load: an import "
        "that fails, a syntax error, a missing name."
    )


def _owned(case: JUnitCase, files: Mapping[str, str]) -> bool:
    return any(belongs(case.classname, case.name, rel) for rel in files)


def tail(text: str, *, lines: int = TAIL_LINES, chars: int = TAIL_CHARS) -> str:
    """The last `lines` lines of `text`, at most `chars` characters, starting at a whole line
    when it can."""
    kept = "\n".join(text.rstrip().splitlines()[-lines:])
    if len(kept) > chars:
        kept = kept[-chars:]
        if "\n" in kept:
            kept = kept.split("\n", 1)[1]
    return kept


def _listed(items: Sequence[str]) -> str:
    shown = "; ".join(items[:MAX_LISTED])
    more = len(items) - MAX_LISTED
    return f"{shown}; and {more} more" if more > 0 else shown
