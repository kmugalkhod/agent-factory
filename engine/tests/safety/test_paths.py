"""Tests for factory_engine.safety.paths (task 2.8): the file-tool path check."""

import ctypes
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from factory_engine.config import RoleSettings, default_settings
from factory_engine.errors import SafetyError
from factory_engine.run import Role
from factory_engine.safety.paths import PathDecision, PathPolicy, _link_decision, check_file_tool

WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="Windows path rules")
LOCKED = ("tests/test_lock.py",)
# Table role names: the four roles, plus "boot" for the bootstrap builder.
TABLE_ROLES: dict[str, tuple[Role, bool]] = {
    "planner": ("planner", False),
    "tester": ("tester", False),
    "builder": ("builder", False),
    "reviewer": ("reviewer", False),
    "boot": ("builder", True),
}


def _role_settings(role: Role, bootstrap: bool = False) -> RoleSettings:
    settings = default_settings("win32")
    role_settings: RoleSettings = getattr(settings.roles, role)
    if bootstrap:  # the bootstrap builder writes the whole worktree except plan.md
        patch = settings.bootstrap.builder.model_dump(exclude_unset=True)
        role_settings = role_settings.model_copy(update=patch)
    return role_settings


@pytest.fixture
def roots(tmp_path: Path) -> dict[str, Path]:
    worktree = tmp_path / "worktree_folder"
    for folder in ("src", "tests", ".git", ".github", ".claude"):
        (worktree / folder).mkdir(parents=True)
    (worktree / "src" / "app.py").write_text("", encoding="utf-8")
    (worktree / "tests" / "test_lock.py").write_text("", encoding="utf-8")
    (worktree / "factory.yaml").write_text("", encoding="utf-8")
    run = tmp_path / "run"
    run.mkdir()
    (run / "plan.md").write_text("", encoding="utf-8")
    outside = tmp_path / "outside_folder"
    outside.mkdir()
    return {"wt": worktree, "run": run, "out": outside}


def _policy(roots: dict[str, Path], role: Role, bootstrap: bool = False) -> PathPolicy:
    return PathPolicy.build(
        role,
        _role_settings(role, bootstrap),
        worktree=roots["wt"],
        run_folder=roots["run"],
        locked=LOCKED,
    )


def _check(
    roots: dict[str, Path], role: Role, tool: str, tool_input: dict[str, object], **kw: bool
) -> PathDecision:
    filled = {
        key: value.format(**{k: str(v) for k, v in roots.items()})
        if isinstance(value, str)
        else value
        for key, value in tool_input.items()
    }
    return check_file_tool(_policy(roots, role, **kw), tool, filled)


def _assert(decision: PathDecision, allowed: bool, reason: str) -> None:
    assert decision.allowed is allowed, decision.reason
    if not allowed:
        assert decision.reason.strip()
        assert reason.lower() in decision.reason.lower()


# Each case: role, tool, tool input ({wt}, {run} and {out} are filled in), allowed, a word the
# block reason must contain. Roles use the default settings; "boot" is the bootstrap builder.
CASES = [
    # ---- reads inside the working folders
    ("builder", "Read", {"file_path": "src/app.py"}, True, ""),
    ("builder", "Read", {"file_path": "{wt}/src/app.py"}, True, ""),
    ("reviewer", "Read", {"file_path": "tests/test_lock.py"}, True, ""),
    ("tester", "Read", {"file_path": "{run}/plan.md"}, True, ""),
    ("planner", "Read", {"file_path": "./src/../src/app.py"}, True, ""),
    # ---- reads outside
    ("builder", "Read", {"file_path": "../outside_folder/secret.txt"}, False, "outside"),
    ("builder", "Read", {"file_path": "src/../../outside_folder/x.txt"}, False, "outside"),
    ("builder", "Read", {"file_path": "{out}/x.txt"}, False, "outside"),
    ("builder", "Read", {"file_path": "{wt}/../outside_folder/x.txt"}, False, "outside"),
    ("builder", "Read", {"file_path": "~/.ssh/id_rsa"}, False, "home"),
    ("builder", "Read", {"file_path": ".env"}, False, "secret"),
    ("tester", "Read", {"file_path": "config/.env.local"}, False, "secret"),
    # ---- bad input
    ("builder", "Read", {"file_path": ""}, False, "path"),
    ("builder", "Read", {}, False, "path"),
    ("builder", "Read", {"file_path": 42}, False, "path"),
    ("builder", "Bash", {"command": "ls"}, False, "file tool"),
    # ---- Glob and Grep
    ("builder", "Glob", {"pattern": "**/*.py"}, True, ""),
    ("builder", "Glob", {"pattern": "*.py", "path": "src"}, True, ""),
    ("builder", "Glob", {"pattern": "*", "path": "{out}"}, False, "outside"),
    ("builder", "Glob", {"pattern": "../**/*"}, False, ".."),
    ("builder", "Glob", {"pattern": "{out}/**"}, False, "relative"),
    ("builder", "Grep", {"pattern": "x", "path": "src", "glob": "*.py"}, True, ""),
    ("builder", "Grep", {"pattern": "x"}, True, ""),
    ("builder", "Grep", {"pattern": "x", "path": "../outside_folder"}, False, "outside"),
    ("builder", "Grep", {"pattern": "x", "glob": "../../**"}, False, ".."),
    # ---- tester writes
    ("tester", "Write", {"file_path": "tests/test_new.py"}, True, ""),
    ("tester", "Edit", {"file_path": "tests/test_lock.py"}, True, ""),
    ("tester", "Write", {"file_path": "src/app.py"}, False, "src/**"),
    ("tester", "Write", {"file_path": "tests/../src/app.py"}, False, "src/**"),
    ("tester", "Write", {"file_path": "{run}/handoff-tester.md"}, True, ""),
    ("tester", "Write", {"file_path": "{run}/plan.md"}, False, "run folder"),
    ("tester", "Write", {"file_path": "{run}/handoff-builder.md"}, False, "run folder"),
    # ---- builder writes and the test lock
    ("builder", "Edit", {"file_path": "src/app.py"}, True, ""),
    ("builder", "Write", {"file_path": "src/new/module.py"}, True, ""),
    ("builder", "MultiEdit", {"file_path": "{wt}/src/app.py"}, True, ""),
    ("builder", "Edit", {"file_path": "tests/test_lock.py"}, False, "locked"),
    ("builder", "Write", {"file_path": "tests/test_other.py"}, False, "tests/**"),
    ("builder", "Write", {"file_path": "plan.md"}, False, "denied"),
    ("builder", "Write", {"file_path": "README.md"}, False, "may write only"),
    ("builder", "Write", {"file_path": "../outside_folder/x.py"}, False, "outside"),
    ("builder", "Write", {"file_path": "{run}/plan.md"}, False, "run folder"),
    ("builder", "Write", {"file_path": "{run}/handoff-builder.md"}, True, ""),
    ("builder", "Write", {"file_path": "{run}/attempts/handoff-builder-1.md"}, False, "run folder"),
    # ---- planner and reviewer
    ("planner", "Write", {"file_path": "{run}/plan.md"}, True, ""),
    ("planner", "Write", {"file_path": "{run}/handoff-planner.md"}, True, ""),
    ("planner", "Write", {"file_path": "plan.md"}, False, "may write only"),
    ("planner", "Edit", {"file_path": "src/app.py"}, False, "may write only"),
    ("reviewer", "Write", {"file_path": "{run}/review.md"}, True, ""),
    ("reviewer", "Write", {"file_path": "{run}/handoff-reviewer.md"}, False, "run folder"),
    ("reviewer", "Edit", {"file_path": "src/app.py"}, False, "may write only"),
    ("reviewer", "NotebookEdit", {"notebook_path": "src/nb.ipynb"}, False, "may write only"),
    # ---- protected files, even for the bootstrap builder
    ("boot", "Write", {"file_path": "pyproject.toml"}, True, ""),
    ("boot", "NotebookEdit", {"notebook_path": "notebooks/a.ipynb"}, True, ""),
    ("boot", "Write", {"file_path": ".git"}, False, "protected"),
    ("boot", "Write", {"file_path": ".git/config"}, False, "protected"),
    ("boot", "Write", {"file_path": ".git/hooks/pre-commit"}, False, "protected"),
    ("boot", "Write", {"file_path": ".claude/settings.json"}, False, "protected"),
    ("boot", "Write", {"file_path": ".github/workflows/ci.yml"}, False, "protected"),
    ("boot", "Edit", {"file_path": "factory.yaml"}, False, "protected"),
    ("boot", "Write", {"file_path": "CLAUDE.md"}, False, "protected"),
    ("boot", "Write", {"file_path": "app/.env.production"}, False, "protected"),
    ("boot", "Write", {"file_path": "plan.md"}, False, "denied"),
    ("boot", "Write", {"file_path": "{wt}"}, False, "worktree itself"),
]

WINDOWS_CASES = [
    # ---- drives, network and device paths
    ("builder", "Read", {"file_path": "Z:\\other\\file.txt"}, False, "outside"),
    ("builder", "Read", {"file_path": "\\\\server\\share\\repo\\a.py"}, False, "network"),
    ("builder", "Read", {"file_path": "//server/share/repo/a.py"}, False, "network"),
    ("builder", "Read", {"file_path": "\\\\?\\C:\\Windows\\win.ini"}, False, "network"),
    ("builder", "Read", {"file_path": "C:relative.txt"}, False, "full path"),
    ("builder", "Read", {"file_path": "\\Windows\\win.ini"}, False, "full path"),
    ("builder", "Glob", {"pattern": "C:\\**"}, False, "relative"),
    # ---- mixed slashes and case differences
    ("builder", "Write", {"file_path": "src\\app.py"}, True, ""),
    ("builder", "Write", {"file_path": "src/sub\\module.py"}, True, ""),
    ("builder", "Write", {"file_path": "SRC\\App.py"}, True, ""),
    ("builder", "Edit", {"file_path": "TESTS\\TEST_LOCK.PY"}, False, "locked"),
    ("builder", "Edit", {"file_path": "{wt}\\tests/test_lock.py"}, False, "locked"),
    ("boot", "Write", {"file_path": ".GIT\\config"}, False, "protected"),
    ("boot", "Write", {"file_path": "Factory.YAML"}, False, "protected"),
    ("boot", "Write", {"file_path": "claude.md"}, False, "protected"),
    ("reviewer", "Write", {"file_path": "{run}\\REVIEW.md"}, True, ""),
    # ---- names Windows rewrites: trailing dots and spaces, data streams
    ("boot", "Write", {"file_path": "factory.yaml."}, False, "trailing"),
    ("boot", "Write", {"file_path": "factory.yaml "}, False, "trailing"),
    ("boot", "Write", {"file_path": ".git./config"}, False, "trailing"),
    ("builder", "Write", {"file_path": "src/app.py:evil"}, False, "stream"),
    ("boot", "Write", {"file_path": "CLAUDE.md::$DATA"}, False, "stream"),
]


def _params(cases: Sequence[tuple[str, str, Mapping[str, object], bool, str]]) -> list[object]:
    return [pytest.param(*case, id=f"{case[0]}-{case[1]}-{i}") for i, case in enumerate(cases)]


@pytest.mark.parametrize(("role", "tool", "tool_input", "allowed", "reason"), _params(CASES))
def test_file_tool_rules(
    roots: dict[str, Path],
    role: str,
    tool: str,
    tool_input: dict[str, object],
    allowed: bool,
    reason: str,
) -> None:
    real_role, bootstrap = TABLE_ROLES[role]
    decision = _check(roots, real_role, tool, tool_input, bootstrap=bootstrap)
    _assert(decision, allowed, reason)


@WINDOWS
@pytest.mark.parametrize(
    ("role", "tool", "tool_input", "allowed", "reason"), _params(WINDOWS_CASES)
)
def test_windows_file_tool_rules(
    roots: dict[str, Path],
    role: str,
    tool: str,
    tool_input: dict[str, object],
    allowed: bool,
    reason: str,
) -> None:
    real_role, bootstrap = TABLE_ROLES[role]
    decision = _check(roots, real_role, tool, tool_input, bootstrap=bootstrap)
    _assert(decision, allowed, reason)


def test_table_has_at_least_40_cases() -> None:
    assert len(CASES) + len(WINDOWS_CASES) >= 40


def test_locked_files_are_written_by_the_tester_only(roots: dict[str, Path]) -> None:
    roles: tuple[Role, ...] = ("planner", "builder", "reviewer")
    for role in roles:
        decision = _check(roots, role, "Write", {"file_path": "tests/test_lock.py"})
        _assert(decision, False, "")


# ------------------------------------------------- junctions and symlinks


def _junction(link: Path, target: Path) -> None:
    import _winapi  # Windows only; the stdlib's own junction call

    _winapi.CreateJunction(str(target), str(link))


@WINDOWS
@pytest.mark.parametrize("tool", ["Read", "Write"])
def test_a_junction_out_of_the_worktree_is_blocked(roots: dict[str, Path], tool: str) -> None:
    _junction(roots["wt"] / "src" / "link", roots["out"])
    decision = _check(roots, "builder", tool, {"file_path": "src/link/x.py"})
    _assert(decision, False, "outside")


@WINDOWS
def test_a_junction_into_the_tests_is_blocked_for_the_builder(roots: dict[str, Path]) -> None:
    _junction(roots["wt"] / "src" / "tests_link", roots["wt"] / "tests")
    decision = _check(roots, "builder", "Edit", {"file_path": "src/tests_link/test_lock.py"})
    _assert(decision, False, "locked")


@WINDOWS
def test_a_junction_to_the_worktree_from_outside_is_allowed(roots: dict[str, Path]) -> None:
    _junction(roots["out"] / "back", roots["wt"])
    decision = _check(roots, "builder", "Edit", {"file_path": "{out}/back/src/app.py"})
    _assert(decision, True, "")


def test_a_symlink_out_of_the_worktree_is_blocked(roots: dict[str, Path]) -> None:
    try:
        (roots["wt"] / "src" / "link").symlink_to(roots["out"], target_is_directory=True)
    except OSError as err:
        pytest.skip(f"can't create symlinks here: {err}")
    decision = _check(roots, "builder", "Write", {"file_path": "src/link/x.py"})
    _assert(decision, False, "outside")


# ------------------------------------------------------------ 8.3 names


def _short(path: Path) -> str:
    buffer = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer))  # type: ignore[attr-defined]  # Windows only
    if not buffer.value or buffer.value.lower() == str(path).lower():
        pytest.skip("8.3 short names are turned off on this volume")
    return buffer.value


@WINDOWS
def test_a_short_name_for_a_protected_folder_is_blocked(roots: dict[str, Path]) -> None:
    short = _short(roots["wt"] / ".github")
    decision = _check(roots, "builder", "Write", {"file_path": short + "\\ci.yml"}, bootstrap=True)
    _assert(decision, False, "protected")


@WINDOWS
def test_a_short_name_for_a_locked_test_is_blocked(roots: dict[str, Path]) -> None:
    short = _short(roots["wt"] / "tests" / "test_lock.py")
    _assert(_check(roots, "builder", "Edit", {"file_path": short}), False, "locked")


@WINDOWS
def test_a_short_name_for_the_worktree_is_allowed(roots: dict[str, Path]) -> None:
    short = _short(roots["wt"])
    _assert(_check(roots, "builder", "Write", {"file_path": short + "\\src\\a.py"}), True, "")


@WINDOWS
def test_a_short_name_outside_is_blocked(roots: dict[str, Path]) -> None:
    short = _short(roots["out"])
    _assert(_check(roots, "builder", "Read", {"file_path": short + "\\x.txt"}), False, "outside")


# ------------------------------------------------- searches reach every file


def _secret(roots: dict[str, Path], rel: str = ".env") -> None:
    path = roots["wt"] / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("TOKEN=secret", encoding="utf-8")


@pytest.mark.parametrize(
    "tool_input",
    [
        {"pattern": ".", "glob": "**/.env*", "output_mode": "content"},
        {"pattern": ".", "glob": "**/.env*", "path": "{wt}"},
        {"pattern": ".", "glob": ".env*", "path": "."},
        {"pattern": "TOKEN"},
        {"pattern": "TOKEN", "path": "config"},
        {"pattern": "TOKEN", "glob": "*.{{env,py}}"},
        {"pattern": "TOKEN", "glob": "!*.py"},
    ],
)
def test_a_search_that_can_read_a_secrets_file_is_blocked(
    roots: dict[str, Path], tool_input: dict[str, object]
) -> None:
    _secret(roots, "config/.env.local")
    _assert(_check(roots, "builder", "Grep", tool_input), False, "secret")


@pytest.mark.parametrize(
    "tool_input",
    [
        {"pattern": "TOKEN", "glob": "*.py"},
        {"pattern": "TOKEN", "path": "src"},
        {"pattern": "TOKEN", "glob": "src/**"},
    ],
)
def test_a_search_that_skips_the_secrets_file_is_allowed(
    roots: dict[str, Path], tool_input: dict[str, object]
) -> None:
    _secret(roots, "config/.env.local")
    _assert(_check(roots, "builder", "Grep", tool_input), True, "")


@WINDOWS
def test_a_search_through_a_junction_out_of_the_worktree_is_blocked(
    roots: dict[str, Path],
) -> None:
    _junction(roots["wt"] / "src" / "link", roots["out"])
    _assert(_check(roots, "builder", "Grep", {"pattern": "x"}), False, "outside")


@WINDOWS
def test_a_glob_does_not_excuse_a_junction_out_of_the_worktree(roots: dict[str, Path]) -> None:
    """A glob filters the files inside a linked folder, which the check can't predict."""
    _junction(roots["wt"] / "src" / "link", roots["out"])
    decision = _check(roots, "builder", "Grep", {"pattern": "x", "glob": "*.py"})
    _assert(decision, False, "outside")


def test_a_plain_folder_search_is_allowed(roots: dict[str, Path]) -> None:
    (roots["wt"] / "src" / "pkg").mkdir()
    (roots["wt"] / "src" / "pkg" / "mod.py").write_text("x = 1", encoding="utf-8")
    _assert(_check(roots, "builder", "Grep", {"pattern": "x", "path": "src"}), True, "")


@pytest.mark.parametrize(
    ("target", "is_file", "glob", "allowed", "reason"),
    [
        ("{out}/notes.txt", True, "*.py", True, ""),
        ("{out}/notes.txt", True, "src/*.py", True, ""),
        ("{out}/notes.txt", True, "*.txt", False, "outside"),
        ("{out}/notes.txt", True, None, False, "outside"),
        ("{out}/notes.txt", True, "*.{py,txt}", False, "outside"),
        ("{out}", False, "*.py", False, "outside"),
        ("{wt}/config/.env", True, "*.py", True, ""),
        ("{wt}/config/.env", True, "*.txt", False, "secret"),
        ("{wt}/config/.env", True, None, False, "secret"),
        ("{wt}/src/app.py", True, None, True, ""),
        ("{wt}/tests", False, None, True, ""),
    ],
)
def test_each_link_a_search_meets_is_judged_by_its_target_and_the_glob(
    roots: dict[str, Path],
    target: str,
    is_file: bool,
    glob: str | None,
    allowed: bool,
    reason: str,
) -> None:
    """The judgement behind the link tests below, which need symlink rights to run."""
    policy = _policy(roots, "builder")
    link = roots["wt"] / "src" / "notes.txt"
    resolved = Path(target.format(**{k: str(v) for k, v in roots.items()})).resolve()
    blocked = _link_decision(policy, link, "src/notes.txt", resolved, is_file, glob)
    _assert(blocked or PathDecision(True), allowed, reason)


def _file_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except OSError as err:
        pytest.skip(f"can't create symlinks here: {err}")


@pytest.mark.parametrize(
    ("glob", "allowed"),
    [("*.py", True), ("src/*.py", True), ("*.txt", False), (None, False), ("*.{py,txt}", False)],
)
def test_a_file_link_out_is_judged_by_the_glob(
    roots: dict[str, Path], glob: str | None, allowed: bool
) -> None:
    _file_link(roots["wt"] / "src" / "notes.txt", roots["out"] / "notes.txt")
    tool_input: dict[str, object] = {"pattern": "x"}
    if glob is not None:
        tool_input["glob"] = glob
    _assert(_check(roots, "builder", "Grep", tool_input), allowed, "outside")


@pytest.mark.parametrize(("glob", "allowed"), [("*.py", True), ("*.txt", False), (None, False)])
def test_a_file_link_to_a_secrets_file_is_judged_by_the_glob(
    roots: dict[str, Path], glob: str | None, allowed: bool
) -> None:
    _secret(roots, "config/.env")
    _file_link(roots["wt"] / "src" / "notes.txt", roots["wt"] / "config" / ".env")
    tool_input: dict[str, object] = {"pattern": "x", "path": "src"}
    if glob is not None:
        tool_input["glob"] = glob
    _assert(_check(roots, "builder", "Grep", tool_input), allowed, "secret")


# --------------------------------------------------------- .git as a file


def test_a_linked_worktree_git_file_is_protected(roots: dict[str, Path]) -> None:
    (roots["wt"] / ".git").rmdir()
    (roots["wt"] / ".git").write_text("gitdir: C:/repos/app/.git/worktrees/x", encoding="utf-8")
    for tool in ("Write", "Edit"):
        decision = _check(roots, "builder", tool, {"file_path": ".git"}, bootstrap=True)
        _assert(decision, False, "protected")


# ---------------------------------------------------- lock entries resolve


def _boot_policy(roots: dict[str, Path], locked: tuple[str, ...]) -> PathPolicy:
    return PathPolicy.build(
        "builder",
        _role_settings("builder", bootstrap=True),
        worktree=roots["wt"],
        run_folder=roots["run"],
        locked=locked,
    )


@pytest.mark.parametrize(
    "entry", ["tests/../tests/test_lock.py", "./tests/test_lock.py", "tests//test_lock.py"]
)
def test_a_lock_entry_locks_the_real_file(roots: dict[str, Path], entry: str) -> None:
    policy = _boot_policy(roots, (entry,))
    decision = check_file_tool(policy, "Edit", {"file_path": "tests/test_lock.py"})
    _assert(decision, False, "locked")


@WINDOWS
def test_a_lock_entry_through_a_junction_locks_the_real_file(roots: dict[str, Path]) -> None:
    _junction(roots["wt"] / "alias", roots["wt"] / "tests")
    policy = _boot_policy(roots, ("alias/test_lock.py",))
    decision = check_file_tool(policy, "Edit", {"file_path": "tests/test_lock.py"})
    _assert(decision, False, "locked")


@pytest.mark.parametrize("entry", ["../outside_folder/x.py", "."])
def test_a_lock_entry_outside_the_worktree_is_refused(roots: dict[str, Path], entry: str) -> None:
    with pytest.raises(SafetyError) as exc:
        _boot_policy(roots, (entry,))
    assert entry in str(exc.value)


# ------------------------------------------------------------ link loops


def test_a_path_that_cannot_resolve_is_blocked(
    roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = _policy(roots, "builder")

    def loop(self: Path, strict: bool = False) -> Path:
        raise RuntimeError(f"Symlink loop from {self}")

    monkeypatch.setattr(Path, "resolve", loop)
    _assert(check_file_tool(policy, "Read", {"file_path": "src/a"}), False, "resolve")


def test_a_symlink_loop_is_blocked(roots: dict[str, Path]) -> None:
    a, b = roots["wt"] / "src" / "a", roots["wt"] / "src" / "b"
    try:
        a.symlink_to(b)
        b.symlink_to(a)
    except OSError as err:
        pytest.skip(f"can't create symlinks here: {err}")
    _assert(_check(roots, "builder", "Read", {"file_path": "src/a"}), False, "")
