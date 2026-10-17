"""The PreToolUse hook: one Agent SDK callback that runs every safety check on a tool call.

- File tools (Read, Write, Edit, MultiEdit, NotebookEdit, Glob, Grep) go through the path check,
  with relative paths from the session's folder (`cwd`); a call that writes text then goes
  through the secret scan.
- Bash and PowerShell go through the shell check, run from the session's folder (`cwd`).
- Skill passes; each one loaded is logged as a `skill_loaded` event. Which skills an agent can
  load is set by its config folder.
- A few tools that touch no files, shell or network pass (TodoWrite, subagents, background
  shell output). Web tools are blocked: nothing reaches the network. Any other tool is blocked.

Every decision is logged as a `tool_call` event before the agent sees it, with secrets in its
target and reason masked. A blocked call gets a
deny decision with the check's reason; an allowed call gets no decision, so Claude Code's own
permission rules still apply. The hook fails closed: bad input, an exception in a check or an
event that can't be logged all deny the call.
"""

import logging
import sys
from collections.abc import Callable, Coroutine, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Protocol

from factory_engine.config import Settings
from factory_engine.events import Body, SkillLoaded, ToolCall
from factory_engine.run import Role
from factory_engine.safety.commands import SHELL_TOOLS, ShellPolicy, check_shell_command
from factory_engine.safety.paths import FILE_TOOLS, PathPolicy, check_file_tool
from factory_engine.safety.secrets import check_tool_content, redact

logger = logging.getLogger(__name__)

HOOK_EVENT = "PreToolUse"
SKILL_TOOL = "Skill"
# Tools that read or write nothing on disk, run nothing and reach no network.
PASS_TOOLS = frozenset({"TodoWrite", "Task", "Agent", "BashOutput", "KillShell", "KillBash"})
NETWORK_TOOLS = frozenset({"WebFetch", "WebSearch"})
WRITE_TOOLS = frozenset(name for name, (_, writes, _) in FILE_TOOLS.items() if writes)
MAX_TARGET = 500  # characters of a target kept in the event

# The SDK's HookCallback shape: (input, tool use ID, context) -> output. Typed loosely here so
# the engine needn't import the SDK; the runner registers it under a PreToolUse HookMatcher.
HookCallback = Callable[
    [Mapping[str, object], str | None, object], Coroutine[None, None, dict[str, object]]
]


class EventSink(Protocol):
    """Where decisions go: an `EventLog`, or a stand-in in tests."""

    def append(self, body: Body, *, role: Role | None = None) -> object: ...


@dataclass(frozen=True)
class HookPolicy:
    """Everything the hook checks for one role in one run. Build it with `HookPolicy.build`."""

    role: Role
    paths: PathPolicy
    shell: ShellPolicy

    @classmethod
    def build(
        cls,
        role: Role,
        settings: Settings,
        *,
        worktree: Path,
        run_folder: Path,
        locked: Iterable[str] = (),
        read_only: Iterable[Path] = (),
        windows: bool = sys.platform == "win32",
    ) -> "HookPolicy":
        """`settings` are the run's effective settings (bootstrap applied for run 0); `locked`
        holds the worktree-relative paths of the files the tester created; `read_only` the
        folders file tools may read but never write (the agent's skills)."""
        return cls(
            role=role,
            paths=PathPolicy.build(
                role,
                getattr(settings.roles, role),
                worktree=worktree,
                run_folder=run_folder,
                locked=locked,
                read_only=read_only,
                windows=windows,
            ),
            shell=ShellPolicy.build(
                role, settings, worktree=worktree, run_folder=run_folder, windows=windows
            ),
        )


@dataclass(frozen=True)
class _Decision:
    tool: str
    target: str | None
    reason: str | None  # None: allowed
    skill: str | None = None


def make_safety_hook(policy: HookPolicy, events: EventSink) -> HookCallback:
    """The PreToolUse callback for one agent, logging to that run's events."""

    async def hook(
        input_data: Mapping[str, object], tool_use_id: str | None, context: object
    ) -> dict[str, object]:
        try:
            decision = _decide(policy, input_data)
        except Exception as err:  # fail closed: a broken check blocks the call
            logger.exception("safety hook failed for %s", tool_use_id)
            decision = _Decision(
                _tool_name(input_data),
                None,
                f"Blocked: the safety check failed ({type(err).__name__}: {err}). "
                "Try the call another way; if it keeps failing, say so in your handoff.",
            )
        if decision.reason is not None:  # a reason may quote the command
            decision = replace(decision, reason=redact(decision.reason))
        try:
            _log(events, policy.role, decision)
        except Exception as err:  # an unlogged call never runs
            logger.exception("can't log the safety decision for %s", tool_use_id)
            if decision.reason is None:
                decision = _Decision(
                    decision.tool,
                    decision.target,
                    redact(f"Blocked: the call couldn't be logged ({type(err).__name__}: {err})."),
                )
                _log_deny(events, policy.role, decision)
        if decision.reason is None:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": HOOK_EVENT,
                "permissionDecision": "deny",
                "permissionDecisionReason": decision.reason,
            }
        }

    return hook


def _log(events: EventSink, role: Role, decision: _Decision) -> None:
    events.append(
        ToolCall(
            tool=decision.tool,
            target=decision.target,
            decision="allowed" if decision.reason is None else "blocked",
            reason=decision.reason,
        ),
        role=role,
    )
    if decision.skill is not None:
        events.append(SkillLoaded(skill=decision.skill), role=role)


def _log_deny(events: EventSink, role: Role, decision: _Decision) -> None:
    """After a failed write turned an allowed call into a deny, save the deny too, so the last
    decision logged for the call (an allowed one may be in already) is the one the agent got.
    Best effort: the call is denied either way."""
    try:
        _log(events, role, decision)
    except Exception:
        logger.exception("can't log the deny for %s either", decision.tool)


def _decide(policy: HookPolicy, input_data: object) -> _Decision:
    tool = _tool_name(input_data)
    if not isinstance(input_data, Mapping):
        return _Decision(tool, None, "Blocked: the hook got no tool call to check.")
    if input_data.get("hook_event_name") != HOOK_EVENT:
        return _Decision(tool, None, f"Blocked: the safety hook checks {HOOK_EVENT} calls only.")
    tool_input = input_data.get("tool_input")
    if not isinstance(input_data.get("tool_name"), str) or not isinstance(tool_input, Mapping):
        return _Decision(tool, None, "Blocked: the tool call has no tool name or input.")
    if tool in FILE_TOOLS:
        return _file_tool(policy, tool, tool_input, input_data.get("cwd"))
    if tool in SHELL_TOOLS:
        return _shell_tool(policy, tool, tool_input, input_data.get("cwd"))
    if tool == SKILL_TOOL:
        return _skill(tool_input)
    if tool in PASS_TOOLS:
        return _Decision(tool, None, None)
    if tool in NETWORK_TOOLS:
        return _Decision(tool, _target(tool_input, "url", "query"), _NO_NETWORK)
    return _Decision(
        tool, None, f"Blocked: {tool} isn't a tool the factory's agents use. Use the file tools."
    )


_NO_NETWORK = (
    "Blocked: agents don't reach the network. Work from the code in the worktree; if you need "
    "something from outside, say so in your handoff."
)


def _file_tool(
    policy: HookPolicy, tool: str, tool_input: Mapping[str, object], cwd: object
) -> _Decision:
    path_key, _, pattern_key = FILE_TOOLS[tool]
    target = _target(tool_input, path_key, pattern_key or "pattern", "pattern")
    folder = _cwd(cwd)
    if folder is False:
        return _Decision(tool, target, _NO_CWD)
    # Relative paths and searches without a path start in the session's folder, as the tools do.
    decision = check_file_tool(policy.paths, tool, tool_input, cwd=folder)
    if decision.allowed and tool in WRITE_TOOLS:
        secrets = check_tool_content(tool, tool_input)
        if not secrets.allowed:
            return _Decision(tool, target, secrets.reason)
    return _Decision(tool, target, None if decision.allowed else decision.reason)


def _shell_tool(
    policy: HookPolicy, tool: str, tool_input: Mapping[str, object], cwd: object
) -> _Decision:
    target = _target(tool_input, "command")
    folder = _cwd(cwd)
    if folder is False:
        return _Decision(tool, target, _NO_CWD)
    decision = check_shell_command(policy.shell, tool, tool_input, cwd=folder)
    return _Decision(tool, target, None if decision.allowed else decision.reason)


_NO_CWD = "Blocked: the session's current folder is unknown."


def _cwd(cwd: object) -> Path | None | Literal[False]:
    """The session's folder from the hook input; None when not given, False when unusable."""
    if cwd is None:
        return None
    if not isinstance(cwd, str) or not cwd.strip():
        return False
    return Path(cwd)


def _skill(tool_input: Mapping[str, object]) -> _Decision:
    name = tool_input.get("skill", tool_input.get("command"))
    if not isinstance(name, str) or not name.strip():
        return _Decision(SKILL_TOOL, None, "Blocked: name the skill to load in `skill`.")
    return _Decision(SKILL_TOOL, _shown(name), None, skill=name)


def _tool_name(input_data: object) -> str:
    name = input_data.get("tool_name") if isinstance(input_data, Mapping) else None
    return name if isinstance(name, str) and name else "unknown"


def _target(tool_input: Mapping[str, object], *keys: str) -> str | None:
    """The first text value under `keys`: what the call aims at, as it may be logged."""
    for key in keys:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return _shown(value)
    return None


def _shown(text: str) -> str:
    """`text` with secrets masked, then cut to fit an event (masking first, so a cut can't hide
    a secret from the scan)."""
    text = redact(text)
    return text if len(text) <= MAX_TARGET else text[: MAX_TARGET - 1] + "…"
