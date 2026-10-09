from collections.abc import Callable
from pathlib import Path

import pytest

import dev

Command = list[str]


class StubRunner:
    """Records every command and returns an exit code chosen by `fail`."""

    def __init__(self, fail: Callable[[Command], bool] = lambda cmd: False) -> None:
        self.calls: list[Command] = []
        self.cwds: list[Path] = []
        self._fail = fail

    def __call__(self, cmd: Command, cwd: Path) -> int:
        self.calls.append(list(cmd))
        self.cwds.append(cwd)
        return 1 if self._fail(cmd) else 0


def make_repo(root: Path, parts: tuple[str, ...]) -> Path:
    """Create the given parts under `root` the way dev.py detects them."""
    for part in parts:
        if part == "ui":
            (root / "ui").mkdir(parents=True)
            (root / "ui" / "package.json").write_text("{}", encoding="utf-8")
        else:
            (root / part / "tests").mkdir(parents=True)
            (root / part / "__init__.py").write_text("", encoding="utf-8")
    return root


def run(root: Path, *argv: str, runner: StubRunner | None = None) -> tuple[int, StubRunner]:
    stub = runner if runner is not None else StubRunner()
    code = dev.main(list(argv), runner=stub, root=root)
    return code, stub


PY_PARTS = ("engine", "cli", "tools")


def test_test_engine_runs_only_engine_tests(tmp_path: Path) -> None:
    root = make_repo(tmp_path, PY_PARTS)
    code, stub = run(root, "test", "engine")
    assert code == 0
    assert stub.calls == [["uv", "run", "pytest", "engine/tests"]]


@pytest.mark.parametrize("argv", [("test",), ("test", "all")])
def test_test_without_part_runs_every_existing_part(tmp_path: Path, argv: tuple[str, ...]) -> None:
    root = make_repo(tmp_path, PY_PARTS)
    code, stub = run(root, *argv)
    assert code == 0
    assert stub.calls == [
        ["uv", "run", "pytest", "engine/tests"],
        ["uv", "run", "pytest", "cli/tests"],
        ["uv", "run", "pytest", "tools/tests"],
    ]


def test_commands_run_from_the_repo_root(tmp_path: Path) -> None:
    root = make_repo(tmp_path, PY_PARTS)
    _, stub = run(root, "test")
    assert stub.cwds and all(cwd == root for cwd in stub.cwds)


@pytest.mark.parametrize("action", ["setup", "test", "lint"])
@pytest.mark.parametrize("part", ["engine", "cli", "plugin", "ui"])
def test_missing_part_is_skipped_with_a_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], action: str, part: str
) -> None:
    code, stub = run(tmp_path, action, part)
    assert code == 0
    assert stub.calls == []
    out = capsys.readouterr().out
    assert "skip" in out.lower()
    assert part in out


def test_all_skips_parts_that_do_not_exist_yet(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(tmp_path, PY_PARTS)
    code, stub = run(root, "lint")
    assert code == 0
    out = capsys.readouterr().out.lower()
    assert "skip plugin" in out
    assert "skip ui" in out
    assert not any("plugin" in arg or arg == "ui" for cmd in stub.calls for arg in cmd)


def test_plugin_with_only_skills_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "plugin" / "skills" / "write-plan").mkdir(parents=True)
    (tmp_path / "plugin" / "skills" / "write-plan" / "SKILL.md").write_text("x", encoding="utf-8")
    code, stub = run(tmp_path, "test", "plugin")
    assert code == 0
    assert stub.calls == []


def test_lint_runs_ruff_check_ruff_format_and_pyright(tmp_path: Path) -> None:
    root = make_repo(tmp_path, PY_PARTS)
    code, stub = run(root, "lint", "engine")
    assert code == 0
    assert stub.calls == [
        ["uv", "run", "ruff", "check", "engine"],
        ["uv", "run", "ruff", "format", "--check", "engine"],
        ["uv", "run", "pyright", "engine"],
    ]


def test_lint_all_includes_tools(tmp_path: Path) -> None:
    root = make_repo(tmp_path, PY_PARTS)
    _, stub = run(root, "lint")
    assert ["uv", "run", "pyright", "tools"] in stub.calls


@pytest.mark.parametrize("argv", [("setup",), ("setup", "engine"), ("setup", "all")])
def test_setup_syncs_python_parts_once(tmp_path: Path, argv: tuple[str, ...]) -> None:
    root = make_repo(tmp_path, PY_PARTS)
    code, stub = run(root, *argv)
    assert code == 0
    assert stub.calls == [["uv", "sync"]]


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("setup", ["pnpm", "-C", "ui", "install"]),
        ("test", ["pnpm", "-C", "ui", "test"]),
        ("lint", ["pnpm", "-C", "ui", "lint"]),
    ],
)
def test_ui_uses_pnpm(tmp_path: Path, action: str, expected: Command) -> None:
    root = make_repo(tmp_path, ("ui",))
    code, stub = run(root, action, "ui")
    assert code == 0
    assert stub.calls == [expected]


@pytest.mark.parametrize(
    ("argv", "failing"),
    [
        (("setup",), "sync"),
        (("test",), "cli/tests"),
        (("test", "engine"), "engine/tests"),
        (("lint",), "check"),
        (("lint", "cli"), "--check"),
        (("lint",), "pyright"),
    ],
)
def test_non_zero_exit_is_propagated(tmp_path: Path, argv: tuple[str, ...], failing: str) -> None:
    root = make_repo(tmp_path, PY_PARTS)
    stub = StubRunner(fail=lambda cmd: failing in cmd)
    code, _ = run(root, *argv, runner=stub)
    assert code != 0


def test_a_failure_does_not_stop_the_remaining_parts(tmp_path: Path) -> None:
    root = make_repo(tmp_path, PY_PARTS)
    stub = StubRunner(fail=lambda cmd: "engine/tests" in cmd)
    code, _ = run(root, "test", runner=stub)
    assert code != 0
    assert ["uv", "run", "pytest", "tools/tests"] in stub.calls


@pytest.mark.parametrize("argv", [(), ("build",), ("test", "docs")])
def test_bad_arguments_exit_with_usage_error(tmp_path: Path, argv: tuple[str, ...]) -> None:
    with pytest.raises(SystemExit) as exc:
        run(tmp_path, *argv)
    assert exc.value.code == 2
