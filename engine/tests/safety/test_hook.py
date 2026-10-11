"""Tests for factory_engine.safety.hook (task 2.11): the PreToolUse hook callback."""

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from factory_engine.config import Settings, default_settings
from factory_engine.errors import EventLogError
from factory_engine.events import Body, EventLog, SkillLoaded, ToolCall, read_events
from factory_engine.run import Role
from factory_engine.safety import hook as hook_module
from factory_engine.safety.hook import MAX_TARGET, HookPolicy, make_safety_hook

COMMANDS = {"test": "uv run pytest", "build": "npm run build", "lint": "uv run ruff check ."}
SECRET = "ghp_" + "a1B2c3D4e5F6g7H8i9J0" * 2  # built here so the repo holds no token


def _settings() -> Settings:
    base = default_settings("win32")
    return base.model_copy(update={"commands": base.commands.model_copy(update=COMMANDS)})


@pytest.fixture
def roots(tmp_path: Path) -> dict[str, Path]:
    worktree = tmp_path / "worktree"
    (worktree / "src").mkdir(parents=True)
    (worktree / "src" / "app.py").write_text("def main(): ...\n", encoding="utf-8")
    (worktree / "tests").mkdir()
    (worktree / "tests" / "test_app.py").write_text("def test(): ...\n", encoding="utf-8")
    run = tmp_path / "run"
    run.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "notes.txt").write_text("x", encoding="utf-8")
    return {"wt": worktree, "run": run, "out": outside}


def _clock() -> datetime:
    return datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


def _log(roots: dict[str, Path]) -> EventLog:
    return EventLog(roots["run"], repo="repo-a", run_id=7, clock=_clock)


def _policy(roots: dict[str, Path], role: Role) -> HookPolicy:
    return HookPolicy.build(
        role,
        _settings(),
        worktree=roots["wt"],
        run_folder=roots["run"],
        locked=("tests/test_app.py",),
    )


def _input(
    roots: dict[str, Path], tool: str, tool_input: object, cwd: object = None
) -> dict[str, object]:
    """A PreToolUseHookInput as the Agent SDK passes it."""
    return {
        "session_id": "session-1",
        "transcript_path": str(roots["run"] / "transcript.jsonl"),
        "cwd": str(roots["wt"]) if cwd is None else cwd,
        "permission_mode": "dontAsk",
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
        "tool_use_id": "toolu_1",
    }


def _call(
    roots: dict[str, Path],
    role: Role,
    tool: str,
    tool_input: object,
    *,
    cwd: object = None,
    log: EventLog | None = None,
) -> Mapping[str, object]:
    callback = make_safety_hook(_policy(roots, role), log or _log(roots))
    return asyncio.run(callback(_input(roots, tool, tool_input, cwd), "toolu_1", {"signal": None}))


def _denied(output: Mapping[str, object]) -> str:
    """The deny reason of a hook output; fails if the output isn't a deny."""
    specific = output.get("hookSpecificOutput")
    assert isinstance(specific, dict), output
    assert specific.get("hookEventName") == "PreToolUse"
    assert specific.get("permissionDecision") == "deny"
    reason = specific.get("permissionDecisionReason")
    assert isinstance(reason, str) and reason.strip()
    return reason


def _tool_calls(roots: dict[str, Path]) -> list[ToolCall]:
    return [e.body for e in read_events(roots["run"]) if isinstance(e.body, ToolCall)]


# Each case: role, tool, input ({wt}, {out}, {run} filled in), allowed, a word the reason has.
CASES = [
    # file paths (2.8)
    ("builder", "Read", {"file_path": "{wt}/src/app.py"}, True, ""),
    ("planner", "Read", {"file_path": "{out}/notes.txt"}, False, "outside the worktree"),
    ("builder", "Edit", {"file_path": "{wt}/tests/test_app.py", "old_string": "a"}, False, "lock"),
    ("builder", "Write", {"file_path": "{wt}/src/new.py", "content": "x = 1\n"}, True, ""),
    ("reviewer", "Write", {"file_path": "{wt}/src/new.py", "content": "x"}, False, "may write"),
    ("planner", "Glob", {"pattern": "**/*.py"}, True, ""),
    ("planner", "Grep", {"pattern": "main", "path": "{out}"}, False, "outside"),
    # shell commands (2.9)
    ("builder", "Bash", {"command": "uv run pytest"}, True, ""),
    ("builder", "Bash", {"command": "git push"}, False, "git"),
    ("planner", "PowerShell", {"command": "curl https://example.com"}, False, "network"),
    ("reviewer", "Bash", {"command": "ls"}, False, "no shell"),
    # secrets (2.10)
    (
        "builder",
        "Write",
        {"file_path": "{wt}/src/k.py", "content": f"T = '{SECRET}'\n"},
        False,
        "secret",
    ),
    (
        "builder",
        "Edit",
        {"file_path": "{wt}/src/app.py", "old_string": "a", "new_string": SECRET},
        False,
        "secret",
    ),
    # other tools
    ("builder", "TodoWrite", {"todos": []}, True, ""),
    ("builder", "Skill", {"skill": "write-handoff"}, True, ""),
    ("builder", "WebFetch", {"url": "https://example.com", "prompt": "x"}, False, "network"),
    ("builder", "WebSearch", {"query": "x"}, False, "network"),
    ("builder", "mcp__other__tool", {}, False, "isn't a tool"),
]


def _fill(value: object, roots: dict[str, Path]) -> object:
    if isinstance(value, str):
        return value.format(**{k: str(v) for k, v in roots.items()})
    return value


@pytest.mark.parametrize(("role", "tool", "tool_input", "allowed", "reason"), CASES)
def test_decision_table(
    roots: dict[str, Path],
    role: Role,
    tool: str,
    tool_input: dict[str, object],
    allowed: bool,
    reason: str,
) -> None:
    filled = {key: _fill(value, roots) for key, value in tool_input.items()}
    output = _call(roots, role, tool, filled)
    if allowed:
        assert output == {}
    else:
        assert reason.lower() in _denied(output).lower()


def test_blocked_call_returns_the_check_reason(roots: dict[str, Path]) -> None:
    output = _call(roots, "builder", "Bash", {"command": "git log"})
    reason = _denied(output)
    assert reason.startswith("Blocked:")
    assert "git" in reason


def test_allowed_call_passes_through_without_a_decision(roots: dict[str, Path]) -> None:
    # An empty output leaves the call to Claude Code's own permission rules.
    output = _call(roots, "builder", "Read", {"file_path": str(roots["wt"] / "src" / "app.py")})
    assert output == {}


def test_blocked_decision_is_logged(roots: dict[str, Path]) -> None:
    output = _call(roots, "builder", "Bash", {"command": "git push"})
    [event] = read_events(roots["run"])
    assert event.role == "builder"
    assert event.body == ToolCall(
        tool="Bash", target="git push", decision="blocked", reason=_denied(output)
    )


def test_allowed_decision_is_logged(roots: dict[str, Path]) -> None:
    path = str(roots["wt"] / "src" / "app.py")
    _call(roots, "planner", "Read", {"file_path": path})
    [event] = read_events(roots["run"])
    assert event.role == "planner"
    assert event.body == ToolCall(tool="Read", target=path, decision="allowed", reason=None)


def test_every_decision_appears_in_events(roots: dict[str, Path]) -> None:
    log = _log(roots)
    callback = make_safety_hook(_policy(roots, "builder"), log)
    calls = [
        ("Read", {"file_path": str(roots["wt"] / "src" / "app.py")}),
        ("Bash", {"command": "uv run pytest"}),
        ("Bash", {"command": "curl https://example.com"}),
        ("Write", {"file_path": str(roots["out"] / "x.py"), "content": "x"}),
        ("WebFetch", {"url": "https://example.com"}),
        ("Read", "not a dict"),
    ]
    for tool, tool_input in calls:
        asyncio.run(callback(_input(roots, tool, tool_input), "toolu_1", {"signal": None}))
    logged = _tool_calls(roots)
    assert [(e.tool, e.decision) for e in logged] == [
        ("Read", "allowed"),
        ("Bash", "allowed"),
        ("Bash", "blocked"),
        ("Write", "blocked"),
        ("WebFetch", "blocked"),
        ("Read", "blocked"),
    ]
    assert all(e.reason for e in logged if e.decision == "blocked")


def test_skill_logs_skill_loaded(roots: dict[str, Path]) -> None:
    output = _call(roots, "planner", "Skill", {"skill": "write-plan"})
    assert output == {}
    bodies = [e.body for e in read_events(roots["run"])]
    assert bodies == [
        ToolCall(tool="Skill", target="write-plan", decision="allowed", reason=None),
        SkillLoaded(skill="write-plan"),
    ]


def test_skill_without_a_name_is_blocked(roots: dict[str, Path]) -> None:
    assert "skill" in _denied(_call(roots, "planner", "Skill", {})).lower()
    assert not [e for e in read_events(roots["run"]) if isinstance(e.body, SkillLoaded)]


def test_secret_never_appears_in_reason_or_events(roots: dict[str, Path]) -> None:
    path = str(roots["wt"] / "src" / "k.py")
    output = _call(roots, "builder", "Write", {"file_path": path, "content": SECRET})
    assert SECRET not in _denied(output)
    assert SECRET not in (roots["run"] / "events.jsonl").read_text(encoding="utf-8")


def test_write_target_is_the_path_not_the_content(roots: dict[str, Path]) -> None:
    path = str(roots["wt"] / "src" / "new.py")
    _call(roots, "builder", "Write", {"file_path": path, "content": "x = 1\n"})
    [event] = _tool_calls(roots)
    assert event.target == path


def test_long_target_is_cut(roots: dict[str, Path]) -> None:
    command = "uv run pytest " + "-k a " * 400
    _call(roots, "builder", "Bash", {"command": command})
    [event] = _tool_calls(roots)
    assert event.target is not None
    assert len(event.target) <= MAX_TARGET
    assert command.startswith(event.target.removesuffix("…"))


def test_shell_in_a_folder_outside_the_worktree_is_blocked(roots: dict[str, Path]) -> None:
    output = _call(roots, "builder", "Bash", {"command": "uv run pytest"}, cwd=str(roots["out"]))
    assert "outside the worktree" in _denied(output)


def test_shell_in_a_worktree_subfolder_is_allowed(roots: dict[str, Path]) -> None:
    cwd = str(roots["wt"] / "src")
    assert _call(roots, "builder", "Bash", {"command": "uv run pytest"}, cwd=cwd) == {}


# ---------------------------------------------------------------- fail closed


@pytest.mark.parametrize(
    "hook_input",
    [
        {"hook_event_name": "PreToolUse", "tool_input": {}},  # no tool name
        {"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": ["x"]},
        {"hook_event_name": "PostToolUse", "tool_name": "Read", "tool_input": {}},
        {"hook_event_name": "PreToolUse", "tool_name": 3, "tool_input": {}},
        "not a mapping",
    ],
)
def test_malformed_input_is_denied(roots: dict[str, Path], hook_input: object) -> None:
    callback = make_safety_hook(_policy(roots, "builder"), _log(roots))
    output = asyncio.run(callback(hook_input, None, None))  # type: ignore[arg-type]  # bad input on purpose
    _denied(output)
    [event] = _tool_calls(roots)
    assert event.decision == "blocked"


def test_exception_in_a_check_denies_the_call(
    roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("check exploded")

    monkeypatch.setattr(hook_module, "check_file_tool", boom)
    output = _call(roots, "builder", "Read", {"file_path": str(roots["wt"] / "src" / "app.py")})
    reason = _denied(output)
    assert "check exploded" in reason
    [event] = _tool_calls(roots)
    assert event.decision == "blocked"
    assert event.reason == reason


def test_exception_in_the_shell_cwd_denies_the_call(roots: dict[str, Path]) -> None:
    output = _call(roots, "builder", "Bash", {"command": "uv run pytest"}, cwd=12)
    _denied(output)


class _BrokenLog:
    """An event log whose writes fail."""

    def __init__(self) -> None:
        self.calls = 0

    def append(self, body: Body, *, role: Role | None = None) -> object:
        self.calls += 1
        raise EventLogError("disk full")


def test_allowed_call_is_denied_when_it_cant_be_logged(roots: dict[str, Path]) -> None:
    log = _BrokenLog()
    callback = make_safety_hook(_policy(roots, "builder"), log)
    hook_input = _input(roots, "Read", {"file_path": str(roots["wt"] / "src" / "app.py")})
    output = asyncio.run(callback(hook_input, "toolu_1", None))
    assert "log" in _denied(output).lower()
    assert log.calls >= 1


def test_blocked_call_stays_denied_when_it_cant_be_logged(roots: dict[str, Path]) -> None:
    callback = make_safety_hook(_policy(roots, "builder"), _BrokenLog())
    output = asyncio.run(callback(_input(roots, "Bash", {"command": "git push"}), None, None))
    assert "git" in _denied(output)


def test_skill_is_denied_when_skill_loaded_cant_be_logged(roots: dict[str, Path]) -> None:
    class _FailOnSkill:
        def __init__(self) -> None:
            self.saved: list[Body] = []

        def append(self, body: Body, *, role: Role | None = None) -> object:
            if isinstance(body, SkillLoaded):
                raise EventLogError("disk full")
            self.saved.append(body)
            return None

    log = _FailOnSkill()
    callback = make_safety_hook(_policy(roots, "planner"), log)
    output = asyncio.run(callback(_input(roots, "Skill", {"skill": "write-plan"}), None, None))
    reason = _denied(output)
    # The last saved decision for the call is the deny the agent got.
    last = [body for body in log.saved if isinstance(body, ToolCall)][-1]
    assert last == ToolCall(tool="Skill", target="write-plan", decision="blocked", reason=reason)


# ---------------------------------------------------------------- the session's folder


@pytest.mark.parametrize("tool", ["Read", "Grep"])
def test_relative_file_path_is_checked_from_the_cwd(roots: dict[str, Path], tool: str) -> None:
    # app.py exists only in src/; from the worktree root it would be judged as another file.
    key = "file_path" if tool == "Read" else "path"
    tool_input = {key: "app.py", "pattern": "main"}
    assert _call(roots, "planner", tool, tool_input, cwd=str(roots["wt"] / "src")) == {}
    # From a folder outside, the same relative path names a file outside the worktree.
    output = _call(
        roots, "planner", tool, {key: "notes.txt", "pattern": "x"}, cwd=str(roots["out"])
    )
    assert "outside" in _denied(output)


def test_search_without_a_path_checks_the_cwd(roots: dict[str, Path]) -> None:
    output = _call(roots, "planner", "Glob", {"pattern": "*.txt"}, cwd=str(roots["out"]))
    assert "outside" in _denied(output)


@pytest.mark.parametrize("cwd", [12, "", "src"])
def test_file_tool_with_a_bad_cwd_is_blocked(roots: dict[str, Path], cwd: object) -> None:
    output = _call(roots, "planner", "Read", {"file_path": "src/app.py"}, cwd=cwd)
    assert "current folder" in _denied(output)


# ---------------------------------------------------------------- secrets in targets


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("WebFetch", {"url": f"https://api.example.com/?access_token={SECRET}", "prompt": "x"}),
        ("Bash", {"command": f"curl -u bob:{SECRET} https://example.com"}),
        ("Bash", {"command": f"git clone https://bob:{SECRET}@github.com/x/y"}),
        ("Bash", {"command": f"API_TOKEN={SECRET} uv run pytest"}),
        ("Grep", {"pattern": SECRET}),
    ],
)
def test_secret_in_a_target_is_never_logged(
    roots: dict[str, Path], tool: str, tool_input: dict[str, object]
) -> None:
    output = _call(roots, "builder", tool, tool_input)
    if output:
        assert SECRET not in _denied(output)
    assert SECRET not in (roots["run"] / "events.jsonl").read_text(encoding="utf-8")
    [event] = _tool_calls(roots)
    assert event.target and "…" in event.target
