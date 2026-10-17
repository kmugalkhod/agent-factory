"""Tests for factory_engine.agents.fake (task 2.12): `FakeAgentRunner` and the session recorder.

The fake replays scripted SDK messages through the same `SessionRecorder` the real runner
uses, so these tests cover how messages become events, session IDs and token totals.
"""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from factory_engine.agents.fake import (
    HOLD,
    FakeAgentRunner,
    Step,
    assistant_text,
    result,
    system_init,
)
from factory_engine.agents.runner import AgentResult, AgentSpec, SessionRecorder
from factory_engine.errors import AgentError
from factory_engine.events import EventLog, Message, Tokens, read_events
from factory_engine.registry import Registry
from factory_engine.run import Role, Run, TokenUse, load_run, new_run, save_run

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


def _clock() -> datetime:
    return NOW


async def _no_hook(
    input_data: object, tool_use_id: str | None, context: object
) -> dict[str, object]:
    return {}


def _spec(
    tmp_path: Path, role: Role = "builder", attempt: int = 1, resume: str | None = None
) -> AgentSpec:
    return AgentSpec(
        role=role,
        attempt=attempt,
        prompt="Do the task.",
        worktree=tmp_path / "wt",
        run_folder=tmp_path / "run",
        system_prompt="You are the builder.",
        model="claude-sonnet-5-5",
        effort="normal",
        env={"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")},
        tools=("Read",),
        skills=None,
        resume=resume,
        max_turns=10,
        hook=_no_hook,
    )


class World:
    """A saved run with its event log and registry, as the engine would hold them."""

    def __init__(self, tmp_path: Path) -> None:
        self.folder = tmp_path / "run"
        self.folder.mkdir()
        self.run = new_run(
            run_id=3,
            slug="add-login",
            repo="repo-a",
            kind="feature",
            provider="subscription",
            isolation="worktree",
            now=NOW,
        )
        save_run(self.run, self.folder)
        self.events = EventLog(self.folder, repo="repo-a", run_id=3, clock=_clock)
        self.registry = Registry(tmp_path / "registry.db")
        self.registry.upsert_run(self.run)

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

    def play(
        self, runner: FakeAgentRunner, spec: AgentSpec, role: Role, attempt: int
    ) -> AgentResult:
        outcome = asyncio.run(runner.run(spec, self.recorder(role, attempt)))
        self.run = outcome.run
        return outcome


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    w = World(tmp_path)
    yield w
    w.registry.close()


def test_session_id_is_saved_to_run_json_and_registry(world: World, tmp_path: Path) -> None:
    runner = FakeAgentRunner([[system_init("sess-1"), result("sess-1", 10, 5)]])

    outcome = world.play(runner, _spec(tmp_path), "builder", 1)

    assert outcome.session_id == "sess-1"
    assert load_run(world.folder).session_ids == {"builder": "sess-1"}
    indexed = world.registry.get_run("repo-a", 3)
    assert indexed is not None
    assert indexed.session_ids == {"builder": "sess-1"}
    sessions = world.registry.list_sessions("repo-a", 3)
    assert [(s.role, s.session_id) for s in sessions] == [("builder", "sess-1")]


def test_session_id_from_the_result_alone_is_saved(world: World, tmp_path: Path) -> None:
    runner = FakeAgentRunner([[result("sess-9", 1, 1)]])

    world.play(runner, _spec(tmp_path), "builder", 1)

    assert load_run(world.folder).session_ids == {"builder": "sess-9"}


def test_tokens_per_role_and_attempt_equal_the_scripted_totals(
    world: World, tmp_path: Path
) -> None:
    script: dict[tuple[Role, int], list[list[Step]]] = {
        ("planner", 1): [[system_init("p"), result("p", 100, 20, cache_read=7)]],
        ("builder", 1): [[system_init("b"), result("b", 300, 40, cache_creation=3)]],
        # Attempt 2 resumes the builder; two turns (a message was sent mid-run).
        ("builder", 2): [[result("b", 50, 5), HOLD], [result("b", 60, 6, cache_read=1)]],
    }
    for (role, attempt), turns in script.items():
        runner = FakeAgentRunner(turns)
        spec = _spec(tmp_path, role, attempt)
        if len(turns) == 1:
            world.play(runner, spec, role, attempt)
        else:

            async def run_and_send(
                runner: FakeAgentRunner = runner,
                spec: AgentSpec = spec,
                role: Role = role,
                attempt: int = attempt,
            ) -> AgentResult:
                task = asyncio.ensure_future(runner.run(spec, world.recorder(role, attempt)))
                await runner.started.wait()
                await runner.send("one more thing")
                return await task

            world.run = asyncio.run(run_and_send()).run

    tokens = [e for e in read_events(world.folder) if isinstance(e.body, Tokens)]
    totals: dict[tuple[str | None, int], tuple[int, int, int, int]] = {}
    for event in tokens:
        assert isinstance(event.body, Tokens)
        b = event.body
        old = totals.get((event.role, b.attempt), (0, 0, 0, 0))
        totals[(event.role, b.attempt)] = (
            old[0] + b.input,
            old[1] + b.output,
            old[2] + b.cache_read,
            old[3] + b.cache_creation,
        )
    assert totals == {
        ("planner", 1): (100, 20, 7, 0),
        ("builder", 1): (300, 40, 0, 3),
        ("builder", 2): (110, 11, 1, 0),
    }
    saved = load_run(world.folder)
    assert saved.metrics.tokens == {"planner": 127, "builder": 343 + 121 + 1}
    indexed = world.registry.get_run("repo-a", 3)
    assert indexed is not None
    assert indexed.metrics.tokens == saved.metrics.tokens


def test_each_result_is_kept_as_dated_token_use_of_its_attempt(
    world: World, tmp_path: Path
) -> None:  # 2.27
    world.play(
        FakeAgentRunner([[result("p", 100, 20, cache_read=7)]]),
        _spec(tmp_path, "planner", 1),
        "planner",
        1,
    )
    world.play(
        FakeAgentRunner([[result("b", 30, 4, cache_creation=2)]]),
        _spec(tmp_path, "builder", 2),
        "builder",
        2,
    )
    saved = load_run(world.folder)
    assert saved.metrics.token_use == [
        TokenUse(role="planner", attempt=1, at=NOW, input=100, output=20, cache_read=7),
        TokenUse(role="builder", attempt=2, at=NOW, input=30, output=4, cache_creation=2),
    ]
    assert saved.metrics.tokens == {"planner": 127, "builder": 36}
    assert world.registry.get_run("repo-a", 3) == saved


def test_outcome_usage_is_this_attempts_total(world: World, tmp_path: Path) -> None:
    runner = FakeAgentRunner([[result("s", 10, 2, cache_read=3, cache_creation=4)]])

    outcome = world.play(runner, _spec(tmp_path), "builder", 1)

    assert (outcome.usage.input, outcome.usage.output) == (10, 2)
    assert (outcome.usage.cache_read, outcome.usage.cache_creation) == (3, 4)
    assert outcome.usage.total == 19


def test_agent_text_is_logged_as_a_message_event(world: World, tmp_path: Path) -> None:
    runner = FakeAgentRunner([[assistant_text("All tests pass."), result("s", 1, 1, "done")]])

    outcome = world.play(runner, _spec(tmp_path), "builder", 1)

    messages = [(e.role, e.body) for e in read_events(world.folder) if isinstance(e.body, Message)]
    assert messages == [("builder", Message(sender="agent", text="All tests pass."))]
    assert outcome.outcome == "completed"
    assert outcome.result == "done"


def test_agent_text_has_secrets_masked(world: World, tmp_path: Path) -> None:
    secret = "ghp_" + "a1B2c3D4e5F6g7H8i9J0" * 2
    runner = FakeAgentRunner([[assistant_text(f"found {secret}"), result("s", 1, 1)]])

    world.play(runner, _spec(tmp_path), "builder", 1)

    assert secret not in (world.folder / "events.jsonl").read_text(encoding="utf-8")


def test_error_result_fails_the_attempt(world: World, tmp_path: Path) -> None:
    runner = FakeAgentRunner([[result("s", 1, 1, is_error=True)]])

    assert world.play(runner, _spec(tmp_path), "builder", 1).outcome == "failed"


def test_stream_without_a_result_fails_the_attempt(world: World, tmp_path: Path) -> None:
    runner = FakeAgentRunner([[system_init("s")]])

    assert world.play(runner, _spec(tmp_path), "builder", 1).outcome == "failed"


def test_interrupt_stops_the_turn(world: World, tmp_path: Path) -> None:
    runner = FakeAgentRunner(
        [[system_init("s"), assistant_text("working"), HOLD, result("s", 9, 9)]]
    )

    async def go() -> AgentResult:
        task = asyncio.ensure_future(runner.run(_spec(tmp_path), world.recorder()))
        await runner.started.wait()
        await runner.interrupt()
        return await task

    outcome = asyncio.run(go())

    assert outcome.outcome == "interrupted"
    assert outcome.usage.total == 0


def test_send_after_interrupt_continues_in_the_same_session(world: World, tmp_path: Path) -> None:
    runner = FakeAgentRunner(
        [
            [system_init("s"), assistant_text("working"), HOLD, result("s", 9, 9)],
            [result("s", 4, 2)],
        ]
    )

    async def go() -> AgentResult:
        task = asyncio.ensure_future(runner.run(_spec(tmp_path), world.recorder()))
        await runner.started.wait()
        await runner.interrupt()
        await runner.send("use the other API")
        return await task

    outcome = asyncio.run(go())

    assert outcome.outcome == "completed"
    assert outcome.session_id == "s"
    assert runner.sent == ["use the other API"]
    messages = [e.body for e in read_events(world.folder) if isinstance(e.body, Message)]
    assert Message(sender="you", text="use the other API") in messages


def test_sent_note_has_secrets_masked_in_the_events(world: World, tmp_path: Path) -> None:
    secret = "ghp_" + "a1B2c3D4e5F6g7H8i9J0" * 2
    runner = FakeAgentRunner([[system_init("s"), HOLD, result("s", 1, 1)], [result("s", 1, 1)]])

    async def go() -> AgentResult:
        task = asyncio.ensure_future(runner.run(_spec(tmp_path), world.recorder()))
        await runner.started.wait()
        await runner.send(f"use the token {secret}")
        return await task

    asyncio.run(go())

    assert runner.sent == [f"use the token {secret}"]  # the agent gets the note as written
    assert secret not in (world.folder / "events.jsonl").read_text(encoding="utf-8")


class _UnloggableNotes(SessionRecorder):
    def sent(self, text: str) -> None:
        raise OSError("disk full")


def test_note_that_cant_be_logged_isnt_waited_for(world: World, tmp_path: Path) -> None:
    runner = FakeAgentRunner([[system_init("s"), HOLD, result("s", 1, 1)]])
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
        task = asyncio.ensure_future(runner.run(_spec(tmp_path), recorder))
        await runner.started.wait()
        with pytest.raises(OSError, match="disk full"):
            await runner.send("use the other API")
        await runner.interrupt()
        return await asyncio.wait_for(task, timeout=5)

    outcome = asyncio.run(go())

    assert outcome.outcome == "interrupted"
    assert runner.sent == []


def test_send_and_interrupt_need_a_running_agent() -> None:
    runner = FakeAgentRunner([])

    with pytest.raises(AgentError, match="no agent is running"):
        asyncio.run(runner.send("hello"))
    with pytest.raises(AgentError, match="no agent is running"):
        asyncio.run(runner.interrupt())


def test_running_out_of_script_raises(world: World, tmp_path: Path) -> None:
    runner = FakeAgentRunner([])

    with pytest.raises(AgentError, match="script"):
        world.play(runner, _spec(tmp_path), "builder", 1)


def test_fake_records_each_spec_it_ran(world: World, tmp_path: Path) -> None:
    runner = FakeAgentRunner([[result("s", 1, 1)]])
    spec = _spec(tmp_path, resume="s")

    world.play(runner, spec, "builder", 1)

    assert runner.specs == [spec]


def test_recorder_rejects_a_spec_for_another_run(world: World, tmp_path: Path) -> None:
    other = world.run.model_copy(update={"id": 4})

    with pytest.raises(AgentError, match="run"):
        SessionRecorder(
            other,
            run_folder=world.folder,
            events=world.events,
            registry=world.registry,
            role="builder",
            attempt=1,
            clock=_clock,
        )


def test_recorder_rejects_attempt_zero(world: World) -> None:
    with pytest.raises(AgentError, match="attempt"):
        world.recorder(attempt=0)


def test_saved_run_keeps_other_fields(world: World, tmp_path: Path) -> None:
    world.run = world.run.model_copy(update={"session_ids": {"planner": "p"}, "stage": "builder"})
    save_run(world.run, world.folder)
    runner = FakeAgentRunner([[result("b", 1, 1)]])

    world.play(runner, _spec(tmp_path), "builder", 1)

    saved: Run = load_run(world.folder)
    assert saved.session_ids == {"planner": "p", "builder": "b"}
    assert saved.stage == "builder"
