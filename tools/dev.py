"""Dev tool: set up, test and lint the repo, or one part of it.

Usage: uv run tools/dev.py {setup,test,lint} [engine|cli|plugin|ui|all]

Parts that don't exist yet are skipped with a message. `test` and `lint` without
a part (or with `all`) also cover `tools/`. Every command runs from the repo root;
a failure doesn't stop the remaining commands, but the exit code is non-zero.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PARTS = ("engine", "cli", "plugin", "ui")
PYTHON_PARTS = ("engine", "cli", "plugin")
ACTIONS = ("setup", "test", "lint")
TIMEOUT_SECONDS = 3600

Runner = Callable[[list[str], Path], int]


def run_command(cmd: list[str], cwd: Path) -> int:
    """Run one command, streaming its output, and return its exit code."""
    exe = shutil.which(cmd[0])
    if exe is None:
        print(f"error: {cmd[0]} not found on PATH; install it and retry", file=sys.stderr)
        return 127
    print(f"> {' '.join(cmd)}", flush=True)
    try:
        result = subprocess.run(
            [exe, *cmd[1:]],
            cwd=cwd,
            check=False,
            timeout=TIMEOUT_SECONDS,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        print(
            f"error: {' '.join(cmd)} took over {TIMEOUT_SECONDS}s and was stopped", file=sys.stderr
        )
        return 124
    return result.returncode


def part_exists(root: Path, part: str) -> bool:
    """A Python part exists once it holds a .py file; the UI once it has package.json."""
    folder = root / part
    if part == "ui":
        return (folder / "package.json").is_file()
    return folder.is_dir() and any(folder.rglob("*.py"))


def python_commands(action: str, part: str) -> list[list[str]]:
    if action == "test":
        return [["uv", "run", "pytest", f"{part}/tests"]]
    return [
        ["uv", "run", "ruff", "check", part],
        ["uv", "run", "ruff", "format", "--check", part],
        ["uv", "run", "pyright", part],
    ]


def ui_command(action: str) -> list[str]:
    return ["pnpm", "-C", "ui", "install" if action == "setup" else action]


def plan(action: str, selected: str, root: Path) -> tuple[list[list[str]], list[str]]:
    """Return the commands to run and the parts skipped because they don't exist yet."""
    parts = list(PARTS) if selected == "all" else [selected]
    commands: list[list[str]] = []
    skipped: list[str] = []
    synced = False
    for part in parts:
        if not part_exists(root, part):
            skipped.append(part)
        elif part == "ui":
            commands.append(ui_command(action))
        elif action == "setup":
            if not synced:
                commands.append(["uv", "sync"])
                synced = True
        else:
            commands.extend(python_commands(action, part))
    if selected == "all" and action != "setup" and part_exists(root, "tools"):
        commands.extend(python_commands(action, "tools"))
    return commands, skipped


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="dev.py", description="Set up, test and lint the repo.")
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("part", nargs="?", default="all", choices=(*PARTS, "all"))
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None, runner: Runner = run_command, root: Path = ROOT) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    commands, skipped = plan(args.action, args.part, root)
    for part in skipped:
        print(f"skip {part}: not created yet")
    failed = [cmd for cmd in commands if runner(cmd, root) != 0]
    for cmd in failed:
        print(f"failed: {' '.join(cmd)}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
