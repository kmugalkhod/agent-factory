"""Tests for factory_engine.agents.runner (task 2.12): agent options and the SDK runner.

`ClaudeAgentRunner` is driven through the real `ClaudeSDKClient` over a fake transport that
speaks the SDK's stream-JSON control protocol, so no CLI or model is involved.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk._internal.transport import Transport

from factory_engine.agents.runner import (
    AgentResult,
    AgentSpec,
    ClaudeAgentRunner,
    SessionRecorder,
    build_spec,
    sdk_options,
)
from factory_engine.config import Settings, default_settings
from factory_engine.errors import AgentError
from factory_engine.events import EventLog, Message, Tokens, ToolCall, read_events
from factory_engine.providers import PROFILES
from factory_engine.registry import Registry
from factory_engine.run import Role, Run, load_run, new_run, save_run
from factory_engine.safety.hook import HookPolicy, make_safety_hook

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
TOKEN = "oauth-token-value"
PARENT = {
    "PATH": r"C:\Windows",
    "SYSTEMROOT": r"C:\Windows",
    "CLAUDE_CODE_OAUTH_TOKEN": TOKEN,
    "ANTHROPIC_API_KEY": "sk-leftover",
    "ANTHROPIC_BASE_URL": "http://proxy.invalid",
    "ANTHROPIC_AUTH_TOKEN": "proxy-token",
    "HTTPS_PROXY": "http://proxy.invalid",
    "CLAUDE_CONFIG_DIR": r"C:\Users\me\.claude",
}
COMMANDS = {"test": "uv run pytest", "lint": "uv run ruff check ."}


def _clock() -> datetime:
    return NOW


def _settings() -> Settings:
    base = default_settings("win32")
    return base.model_copy(update={"commands": base.commands.model_copy(update=COMMANDS)})


@pytest.fixture
def dirs(tmp_path: Path) -> dict[str, Path]:
    worktree = tmp_path / "wt"
    (worktree / "src").mkdir(parents=True)
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    for role in ("planner", "tester", "builder", "reviewer"):
        (prompts / f"{role}.md").write_text(f"# {role.title()}\n\nFactory {role}.\n", "utf-8")
    (prompts / "bootstrap.md").write_text("# Bootstrap addendum\n\nMake it ready.\n", "utf-8")
    run_folder = tmp_path / "run"
    run_folder.mkdir()
    return {
        "wt": worktree,
        "prompts": prompts,
        "run": run_folder,
        "cfg": tmp_path / "cfg",
        "db": tmp_path / "registry.db",
    }


async def _no_hook(
    input_data: object, tool_use_id: str | None, context: object
) -> dict[str, object]:
    return {}


def _run(**update: Any) -> Run:
    run = new_run(
        run_id=3,
        slug="add-login",
        repo="repo-a",
        kind="feature",
        provider="subscription",
        isolation="worktree",
        now=NOW,
    )
    return run.model_copy(update=update)


def _build(dirs: dict[str, Path], role: Role = "builder", **kwargs: Any) -> AgentSpec:
    args: dict[str, Any] = {
        "role": role,
        "attempt": 1,
        "prompt": "Make the tests pass.",
        "run": _run(),
        "settings": _settings(),
        "profile": PROFILES["subscription"],
        "parent_env": PARENT,
        "worktree": dirs["wt"],
        "run_folder": dirs["run"],
        "config_dir": dirs["cfg"],
        "prompts_dir": dirs["prompts"],
        "hook": _no_hook,
        "resume": False,
    }
    args.update(kwargs)
    return build_spec(**args)


# ------------------------------------------------------------------ build_spec


def test_system_prompt_is_role_prompt_plus_repo_agent_prompt(dirs: dict[str, Path]) -> None:
    agents = dirs["wt"] / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "builder.md").write_text(
        "---\nname: builder\nmodel: opus\n---\nUse the repo's logger.\n", encoding="utf-8"
    )

    spec = _build(dirs)

    assert spec.system_prompt.startswith("# Builder\n\nFactory builder.")
    assert "Use the repo's logger." in spec.system_prompt
    assert "model: opus" not in spec.system_prompt  # the model always comes from factory.yaml
    assert spec.system_prompt.index("Factory builder") < spec.system_prompt.index("logger")


def test_system_prompt_without_a_repo_agent_prompt_is_the_role_prompt(
    dirs: dict[str, Path],
) -> None:
    assert _build(dirs, "tester").system_prompt.strip() == "# Tester\n\nFactory tester."


@pytest.mark.parametrize(
    ("role", "added"), [("planner", True), ("builder", True), ("tester", False)]
)
def test_bootstrap_run_adds_the_addendum_to_planner_and_builder(
    dirs: dict[str, Path], role: Role, added: bool
) -> None:
    spec = _build(dirs, role, run=_run(kind="bootstrap", id=0))

    assert ("Bootstrap addendum" in spec.system_prompt) is added


def test_missing_role_prompt_raises(dirs: dict[str, Path]) -> None:
    (dirs["prompts"] / "builder.md").unlink()

    with pytest.raises(AgentError, match=r"builder\.md"):
        _build(dirs)


@pytest.mark.parametrize(
    ("role", "model", "effort"),
    [
        ("planner", "claude-opus-5-5", "high"),
        ("tester", "claude-sonnet-5-5", "normal"),
        ("builder", "claude-sonnet-5-5", "normal"),
        ("reviewer", "claude-opus-5-5", "high"),
    ],
)
def test_model_and_effort_come_from_the_role_and_profile(
    dirs: dict[str, Path], role: Role, model: str, effort: str
) -> None:
    spec = _build(dirs, role)

    assert (spec.model, spec.effort) == (model, effort)


def test_env_is_built_from_a_clean_base(dirs: dict[str, Path]) -> None:
    spec = _build(dirs)

    assert spec.env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert spec.env["CLAUDE_CONFIG_DIR"] == str(dirs["cfg"])
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "HTTPS_PROXY"):
        assert name not in spec.env


def test_resume_takes_the_saved_session_id(dirs: dict[str, Path]) -> None:
    run = _run(session_ids={"builder": "sess-b", "planner": "sess-p"})

    assert _build(dirs, run=run, resume=True).resume == "sess-b"
    assert _build(dirs, run=run, resume=False).resume is None


def test_resume_without_a_saved_session_raises(dirs: dict[str, Path]) -> None:
    with pytest.raises(AgentError, match="no saved session"):
        _build(dirs, "tester", run=_run(session_ids={"builder": "b"}), resume=True)


def test_attempt_must_be_positive(dirs: dict[str, Path]) -> None:
    with pytest.raises(AgentError, match="attempt"):
        _build(dirs, attempt=0)


@pytest.mark.parametrize(
    ("role", "shell"), [("planner", True), ("tester", True), ("builder", True), ("reviewer", False)]
)
def test_tools_offer_the_shell_only_to_roles_with_shell(
    dirs: dict[str, Path], role: Role, shell: bool
) -> None:
    tools = _build(dirs, role).tools

    assert {"Read", "Write", "Edit", "Glob", "Grep", "Skill", "TodoWrite"} <= set(tools)
    assert ("Bash" in tools) is shell
    assert ("PowerShell" in tools) is shell
    assert not {"WebFetch", "WebSearch"} & set(tools)


def test_skills_follow_the_role_list(dirs: dict[str, Path]) -> None:
    settings = _settings()
    roles = settings.roles.model_copy(
        update={"planner": settings.roles.planner.model_copy(update={"skills": ["write-plan"]})}
    )
    settings = settings.model_copy(update={"roles": roles})

    assert _build(dirs, "planner", settings=settings).skills == ("write-plan",)
    assert _build(dirs, "builder", settings=settings).skills is None


# ----------------------------------------------------------------- sdk_options


def test_options_carry_the_spec(dirs: dict[str, Path]) -> None:
    spec = _build(dirs, "planner", run=_run(session_ids={"planner": "sess-p"}), resume=True)

    options = sdk_options(spec, PARENT)

    assert options.cwd == dirs["wt"]
    assert options.system_prompt == {
        "type": "preset",
        "preset": "claude_code",
        "append": spec.system_prompt,
    }
    assert options.model == "claude-opus-5-5"
    assert options.effort == "high"
    assert options.thinking == {"type": "adaptive"}
    assert options.resume == "sess-p"
    assert options.max_turns == 200
    assert options.add_dirs == [dirs["run"]]
    assert options.permission_mode == "dontAsk"
    assert options.setting_sources == ["user", "project"]
    assert options.skills == "all"
    assert options.tools == list(spec.tools)


@pytest.mark.parametrize(
    ("effort", "sdk"), [("low", "low"), ("normal", "medium"), ("high", "high")]
)
def test_effort_maps_to_the_sdk_level(dirs: dict[str, Path], effort: str, sdk: str) -> None:
    settings = _settings()
    roles = settings.roles.model_copy(
        update={"builder": settings.roles.builder.model_copy(update={"effort": effort})}
    )
    spec = _build(dirs, settings=settings.model_copy(update={"roles": roles}))

    assert sdk_options(spec, PARENT).effort == sdk


def test_options_register_the_hook_for_every_pre_tool_use(dirs: dict[str, Path]) -> None:
    spec = _build(dirs)

    hooks = sdk_options(spec, PARENT).hooks

    assert hooks is not None
    assert list(hooks) == ["PreToolUse"]
    [matcher] = hooks["PreToolUse"]
    assert matcher.matcher is None
    assert matcher.hooks == [spec.hook]


def test_parent_variables_are_blanked_in_the_agent_env(dirs: dict[str, Path]) -> None:
    # The SDK merges its env over the engine's own environment, so every parent variable the
    # clean base leaves out must be overridden with an empty value.
    spec = _build(dirs)

    env = sdk_options(spec, PARENT).env

    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "HTTPS_PROXY"):
        assert env[name] == ""
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert env["CLAUDE_CONFIG_DIR"] == str(dirs["cfg"])
    assert env["PATH"] == PARENT["PATH"]
    assert "sk-leftover" not in env.values()
    assert "proxy-token" not in env.values()


def test_parent_variables_are_blanked_whatever_their_case(dirs: dict[str, Path]) -> None:
    spec = _build(dirs)

    env = sdk_options(spec, {**PARENT, "anthropic_api_key": "sk-lower"}).env

    assert env["anthropic_api_key"] == ""


# ------------------------------------------------- ClaudeAgentRunner over the SDK


def _sdk_result(session: str, inp: int, out: int, **extra: Any) -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": "success",
        "duration_ms": 10,
        "duration_api_ms": 8,
        "is_error": False,
        "num_turns": 1,
        "session_id": session,
        "usage": {"input_tokens": inp, "output_tokens": out},
        "result": "done",
        **extra,
    }


def _sdk_init(session: str) -> dict[str, Any]:
    return {"type": "system", "subtype": "init", "session_id": session, "tools": []}


def _sdk_text(text: str, session: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}], "model": "claude-sonnet-5-5"},
        "parent_tool_use_id": None,
        "session_id": session,
    }


WAIT = "wait"  # a turn step: hold until the runner interrupts


class FakeTransport(Transport):
    """Plays the CLI side of the SDK protocol from a script of turns.

    Each user message starts the next turn. A turn's steps are message dicts to emit, a
    `("hook", tool_name, tool_input)` step that calls the registered PreToolUse hook and waits
    for its answer, or `WAIT`, which holds until an interrupt ends the turn.
    """

    def __init__(self, turns: list[list[Any]]) -> None:
        self.turns = list(turns)
        self.written: list[dict[str, Any]] = []
        self.hook_answers: list[dict[str, Any]] = []
        self.started = asyncio.Event()
        self._send, self._receive = anyio.create_memory_object_stream[dict[str, Any]](100)
        self._hook_ids: list[str] = []
        self._answered: dict[str, asyncio.Event] = {}
        self._interrupted = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._ready = False
        self.cwd = "C:/wt"
        self.refuse_notes = False  # fail writing any user message after the first
        self.interrupt_seen = asyncio.Event()
        self.release_interrupts = asyncio.Event()
        self.release_interrupts.set()  # clear it to hold interrupts until it is set again

    async def connect(self) -> None:
        self._ready = True

    async def write(self, data: str) -> None:
        message = json.loads(data)
        notes_refused = self.refuse_notes and any(m["type"] == "user" for m in self.written)
        if message["type"] == "user" and notes_refused:
            raise OSError("pipe closed")
        self.written.append(message)
        if message["type"] == "control_request":
            request = message["request"]
            if request["subtype"] == "initialize":
                for matcher in (request.get("hooks") or {}).get("PreToolUse", []):
                    self._hook_ids += matcher["hookCallbackIds"]
            if request["subtype"] == "interrupt":
                # Answered apart from the write, so a caller that gives up waiting doesn't
                # take the CLI's side of the interrupt with it.
                self.interrupt_seen.set()
                self._tasks.append(asyncio.ensure_future(self._answer_interrupt(message)))
            else:
                await self._answer(message)
        elif message["type"] == "control_response":
            response = message["response"]
            self.hook_answers.append(response)
            self._answered[response["request_id"]].set()
        elif message["type"] == "user":
            turn = self.turns.pop(0)
            self._tasks.append(asyncio.ensure_future(self._play(turn)))

    async def _answer(self, request: dict[str, Any]) -> None:
        await self._emit(
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": request["request_id"],
                    "response": {},
                },
            }
        )

    async def _answer_interrupt(self, request: dict[str, Any]) -> None:
        await self.release_interrupts.wait()
        await self._answer(request)
        self._interrupted.set()

    async def _play(self, turn: list[Any]) -> None:
        self._interrupted.clear()
        for step in turn:
            if step == WAIT:
                self.started.set()
                await self._interrupted.wait()
                await self._emit(_sdk_result("s-1", 1, 1, terminal_reason="aborted_streaming"))
                return
            if isinstance(step, tuple):
                await self._call_hook(step[1], step[2])
            else:
                await self._emit(step)

    async def _call_hook(self, tool: str, tool_input: dict[str, Any]) -> None:
        request_id = f"hook-req-{len(self._answered)}"
        self._answered[request_id] = asyncio.Event()
        await self._emit(
            {
                "type": "control_request",
                "request_id": request_id,
                "request": {
                    "subtype": "hook_callback",
                    "callback_id": self._hook_ids[0],
                    "tool_use_id": "tu-1",
                    "input": {
                        "hook_event_name": "PreToolUse",
                        "session_id": "s-1",
                        "transcript_path": "t.jsonl",
                        "cwd": self.cwd,
                        "tool_name": tool,
                        "tool_input": tool_input,
                    },
                },
            }
        )
        await self._answered[request_id].wait()

    async def _emit(self, message: dict[str, Any]) -> None:
        await self._send.send(message)

    def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        return self._read()

    async def _read(self) -> AsyncIterator[dict[str, Any]]:
        async with self._receive:
            async for message in self._receive:
                yield message

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._ready = False
        await self._send.aclose()

    def is_ready(self) -> bool:
        return self._ready

    async def end_input(self) -> None:
        return None


class World:
    def __init__(self, dirs: dict[str, Path]) -> None:
        self.dirs = dirs
        self.folder = dirs["run"]
        self.run = _run()
        save_run(self.run, self.folder)
        self.events = EventLog(self.folder, repo="repo-a", run_id=3, clock=_clock)
        self.registry = Registry(dirs["db"])
        self.registry.upsert_run(self.run)
        self.options: list[ClaudeAgentOptions] = []

    def recorder(self, role: Role = "builder", attempt: int = 1) -> SessionRecorder:
        return SessionRecorder(
            self.run,
            run_folder=self.folder,
            events=self.events,
            registry=self.registry,
            role=role,
            attempt=attempt,
            clock=_clock,
        )

    def runner(self, transport: FakeTransport) -> ClaudeAgentRunner:
        def client(options: ClaudeAgentOptions) -> ClaudeSDKClient:
            self.options.append(options)
            return ClaudeSDKClient(options, transport=transport)

        return ClaudeAgentRunner(PARENT, client_factory=client)


@pytest.fixture
def world(dirs: dict[str, Path]) -> Iterator[World]:
    w = World(dirs)
    yield w
    w.registry.close()


def test_sdk_run_saves_session_tokens_and_messages(world: World) -> None:
    transport = FakeTransport(
        [[_sdk_init("s-1"), _sdk_text("Done.", "s-1"), _sdk_result("s-1", 120, 30)]]
    )
    runner = world.runner(transport)
    spec = _build(world.dirs)

    outcome = asyncio.run(runner.run(spec, world.recorder()))

    assert outcome.outcome == "completed"
    assert outcome.session_id == "s-1"
    assert load_run(world.folder).session_ids == {"builder": "s-1"}
    indexed = world.registry.get_run("repo-a", 3)
    assert indexed is not None
    assert indexed.session_ids == {"builder": "s-1"}
    bodies = [e.body for e in read_events(world.folder)]
    assert Message(sender="agent", text="Done.") in bodies
    assert Tokens(attempt=1, input=120, output=30) in bodies
    assert load_run(world.folder).metrics.tokens == {"builder": 150}
    [prompt] = [m for m in transport.written if m["type"] == "user"]
    assert prompt["message"]["content"] == "Make the tests pass."


def test_sdk_resume_passes_the_saved_session_id(world: World) -> None:
    first = FakeTransport([[_sdk_init("s-1"), _sdk_result("s-1", 1, 1)]])
    asyncio.run(world.runner(first).run(_build(world.dirs), world.recorder()))
    world.run = load_run(world.folder)

    second = FakeTransport([[_sdk_init("s-1"), _sdk_result("s-1", 1, 1)]])
    spec = _build(world.dirs, attempt=2, run=world.run, resume=True)
    asyncio.run(world.runner(second).run(spec, world.recorder(attempt=2)))

    assert [o.resume for o in world.options] == [None, "s-1"]


def test_sdk_hook_decisions_reach_the_cli_and_the_events(world: World) -> None:
    transport = FakeTransport(
        [
            [
                _sdk_init("s-1"),
                ("hook", "Read", {"file_path": str(world.dirs["wt"] / "src" / "app.py")}),
                ("hook", "WebFetch", {"url": "https://example.com"}),
                _sdk_result("s-1", 1, 1),
            ]
        ]
    )
    transport.cwd = str(world.dirs["wt"])
    policy = HookPolicy.build(
        "builder", _settings(), worktree=world.dirs["wt"], run_folder=world.folder
    )
    spec = _build(world.dirs, hook=make_safety_hook(policy, world.events))

    asyncio.run(world.runner(transport).run(spec, world.recorder()))

    decisions = [
        (e.body.tool, e.body.decision)
        for e in read_events(world.folder)
        if isinstance(e.body, ToolCall)
    ]
    assert decisions == [("Read", "allowed"), ("WebFetch", "blocked")]
    allowed, denied = (a["response"] for a in transport.hook_answers)
    assert "hookSpecificOutput" not in allowed
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_sdk_interrupt_ends_the_turn(world: World) -> None:
    transport = FakeTransport([[_sdk_init("s-1"), WAIT]])
    runner = world.runner(transport)

    async def go() -> AgentResult:
        task = asyncio.ensure_future(runner.run(_build(world.dirs), world.recorder()))
        await transport.started.wait()
        await runner.interrupt()
        return await task

    outcome = asyncio.run(go())

    assert outcome.outcome == "interrupted"
    assert any(
        m["type"] == "control_request" and m["request"]["subtype"] == "interrupt"
        for m in transport.written
    )


def test_sdk_send_continues_the_session_with_the_note(world: World) -> None:
    transport = FakeTransport(
        [[_sdk_init("s-1"), WAIT], [_sdk_text("Switched.", "s-1"), _sdk_result("s-1", 5, 5)]]
    )
    runner = world.runner(transport)

    async def go() -> AgentResult:
        task = asyncio.ensure_future(runner.run(_build(world.dirs), world.recorder()))
        await transport.started.wait()
        await runner.interrupt()
        await runner.send("Use the other API.")
        return await task

    outcome = asyncio.run(go())

    assert outcome.outcome == "completed"
    prompts = [m["message"]["content"] for m in transport.written if m["type"] == "user"]
    assert prompts == ["Make the tests pass.", "Use the other API."]
    bodies = [e.body for e in read_events(world.folder)]
    assert Message(sender="you", text="Use the other API.") in bodies
    assert Message(sender="agent", text="Switched.") in bodies


def test_sdk_send_needs_a_running_agent(world: World) -> None:
    runner = world.runner(FakeTransport([]))

    with pytest.raises(AgentError, match="no agent is running"):
        asyncio.run(runner.send("hello"))


class _UnloggableNotes(SessionRecorder):
    def sent(self, text: str) -> None:
        raise OSError("disk full")


def test_sdk_note_that_cant_be_logged_doesnt_stall_the_run(world: World) -> None:
    transport = FakeTransport([[_sdk_init("s-1"), WAIT]])
    runner = world.runner(transport)
    recorder = _UnloggableNotes(
        world.run,
        run_folder=world.folder,
        events=world.events,
        registry=world.registry,
        role="builder",
        attempt=1,
        clock=_clock,
    )

    async def go() -> AgentResult:
        task = asyncio.ensure_future(runner.run(_build(world.dirs), recorder))
        await transport.started.wait()
        with pytest.raises(OSError, match="disk full"):
            await runner.send("Use the other API.")
        await runner.interrupt()
        return await asyncio.wait_for(task, timeout=5)

    outcome = asyncio.run(go())

    assert outcome.outcome == "interrupted"
    assert [m for m in transport.written if m["type"] == "user"][1:] == []


def test_sdk_note_that_cant_be_sent_doesnt_stall_the_run(world: World) -> None:
    transport = FakeTransport([[_sdk_init("s-1"), WAIT]])
    transport.refuse_notes = True
    runner = world.runner(transport)

    async def go() -> AgentResult:
        task = asyncio.ensure_future(runner.run(_build(world.dirs), world.recorder()))
        await transport.started.wait()
        with pytest.raises(Exception, match="pipe closed"):
            await runner.send("Use the other API.")
        return await asyncio.wait_for(task, timeout=5)

    outcome = asyncio.run(go())

    # The note stopped the turn but never started one, so the attempt ends interrupted.
    assert outcome.outcome == "interrupted"


def test_sdk_note_cancelled_while_interrupting_doesnt_stall_the_run(world: World) -> None:
    transport = FakeTransport([[_sdk_init("s-1"), WAIT]])
    transport.release_interrupts.clear()
    runner = world.runner(transport)

    async def go() -> AgentResult:
        task = asyncio.ensure_future(runner.run(_build(world.dirs), world.recorder()))
        await transport.started.wait()
        sending = asyncio.ensure_future(runner.send("Use the other API."))
        await transport.interrupt_seen.wait()
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending
        transport.release_interrupts.set()
        return await asyncio.wait_for(task, timeout=5)

    outcome = asyncio.run(go())

    assert outcome.outcome == "interrupted"
    assert [m for m in transport.written if m["type"] == "user"][1:] == []


def test_sdk_runner_refuses_a_second_run_at_once(world: World) -> None:
    transport = FakeTransport([[_sdk_init("s-1"), WAIT]])
    runner = world.runner(transport)

    async def go() -> None:
        task = asyncio.ensure_future(runner.run(_build(world.dirs), world.recorder()))
        await transport.started.wait()
        with pytest.raises(AgentError, match="already running"):
            await runner.run(_build(world.dirs), world.recorder())
        await runner.interrupt()
        await task

    asyncio.run(go())


def test_sdk_runner_disconnects_when_recording_fails(world: World) -> None:
    transport = FakeTransport([[_sdk_init("s-1"), _sdk_result("s-1", 1, 1)]])
    runner = world.runner(transport)

    class Broken:
        def upsert_run(self, run: Run) -> None:
            raise OSError("disk full")

    recorder = SessionRecorder(
        world.run,
        run_folder=world.folder,
        events=world.events,
        registry=Broken(),
        role="builder",
        attempt=1,
        clock=_clock,
    )

    with pytest.raises(OSError, match="disk full"):
        asyncio.run(runner.run(_build(world.dirs), recorder))
    assert not transport.is_ready()


def test_options_env_reaches_the_client(world: World) -> None:
    transport = FakeTransport([[_sdk_result("s-1", 1, 1)]])

    asyncio.run(world.runner(transport).run(_build(world.dirs), world.recorder()))

    [options] = world.options
    env: Mapping[str, str] = options.env
    assert env["ANTHROPIC_API_KEY"] == ""
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
