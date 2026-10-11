"""Tests for factory_engine.agents.watchdog (task 2.13): the stall watchdog, usage-limit pause
and the turn and time caps.

Time is a fake clock that only moves when the watchdog sleeps, so a ten-minute stall takes no
real time. The agent is a `FakeAgentRunner`.
"""

import asyncio
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from factory_engine.agents.fake import (
    HOLD,
    FakeAgentRunner,
    Step,
    assistant_text,
    rate_limit,
    result,
    system_init,
)
from factory_engine.agents.runner import AgentResult, AgentSpec, SessionRecorder
from factory_engine.agents.watchdog import (
    FALLBACK_RESET,
    RESUME_PROMPT,
    Limits,
    Monitor,
    Watchdog,
    Watched,
    resume_spec,
    unpause,
    wait_for_reset,
)
from factory_engine.config import default_settings
from factory_engine.errors import AgentError
from factory_engine.events import EventLog, NeedsYou, Paused, StateChanged, read_events
from factory_engine.registry import Registry
from factory_engine.run import Pause, Role, Run, RunState, load_run, new_run, save_run

START = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
LIMITS = Limits(stall=timedelta(minutes=10), max_time=timedelta(minutes=120))


class FakeTime:
    """A clock that moves only when something sleeps on it."""

    def __init__(self, start: datetime) -> None:
        self.now = start
        self.slept: list[float] = []

    def clock(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)
        await asyncio.sleep(0)


async def _no_hook(
    input_data: object, tool_use_id: str | None, context: object
) -> dict[str, object]:
    return {}


def _spec(tmp_path: Path, role: Role = "builder", attempt: int = 1) -> AgentSpec:
    return AgentSpec(
        role=role,
        attempt=attempt,
        prompt="Make the tests pass.",
        worktree=tmp_path / "wt",
        run_folder=tmp_path / "run",
        system_prompt="You are the builder.",
        model="claude-sonnet-5-5",
        effort="normal",
        env={"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")},
        tools=("Read",),
        skills=None,
        resume=None,
        max_turns=200,
        hook=_no_hook,
    )


class World:
    """A building run with its event log and registry, on a fake clock."""

    def __init__(self, tmp_path: Path, state: RunState = "building") -> None:
        self.tmp = tmp_path
        self.time = FakeTime(START)
        self.folder = tmp_path / "run"
        self.folder.mkdir()
        self.run = new_run(
            run_id=3,
            slug="add-login",
            repo="repo-a",
            kind="feature",
            provider="subscription",
            isolation="worktree",
            now=START,
        ).model_copy(update={"state": state, "stage": "builder"})
        save_run(self.run, self.folder)
        self.events = EventLog(self.folder, repo="repo-a", run_id=3, clock=self.time.clock)
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
            clock=self.time.clock,
        )

    def watchdog(self, limits: Limits = LIMITS) -> Watchdog:
        return Watchdog(limits, clock=self.time.clock, sleep=self.time.sleep)

    def watch(self, runner: FakeAgentRunner, spec: AgentSpec, limits: Limits = LIMITS) -> Watched:
        watched = asyncio.run(
            self.watchdog(limits).run(
                runner, spec, self.recorder(spec.role, spec.attempt), events=self.events
            )
        )
        self.run = watched.result.run
        return watched

    def indexed(self) -> Run:
        run = self.registry.get_run("repo-a", 3)
        assert run is not None
        return run


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    w = World(tmp_path)
    yield w
    w.registry.close()


def _bodies(world: World) -> list[object]:
    return [event.body for event in read_events(world.folder)]


# -------------------------------------------------------------------- stall


def test_stall_stops_the_agent_and_needs_you(world: World, tmp_path: Path) -> None:  # AC1
    runner = FakeAgentRunner([[system_init("sess-1"), HOLD, result("sess-1", 1, 1)]])

    watched = world.watch(runner, _spec(tmp_path))

    assert watched.trip is not None
    assert watched.trip.kind == "stall"
    assert watched.result.outcome == "interrupted"
    assert world.time.now - START >= timedelta(minutes=10)
    assert world.time.now - START < timedelta(minutes=11)
    assert load_run(world.folder).state == "needs_you"
    assert world.indexed().state == "needs_you"
    bodies = _bodies(world)
    assert StateChanged(from_state="building", to_state="needs_you") in bodies
    needs = [b for b in bodies if isinstance(b, NeedsYou)]
    assert len(needs) == 1
    assert "10 minutes" in needs[0].reason
    assert "builder" in needs[0].reason


def test_stall_limit_is_configurable(world: World, tmp_path: Path) -> None:
    runner = FakeAgentRunner([[system_init("sess-1"), HOLD]])
    limits = Limits(stall=timedelta(minutes=3), max_time=timedelta(minutes=120))

    watched = world.watch(runner, _spec(tmp_path), limits)

    assert watched.trip is not None and watched.trip.kind == "stall"
    assert timedelta(minutes=3) <= world.time.now - START < timedelta(minutes=4)


def test_limits_come_from_the_caps() -> None:
    caps = default_settings("win32").caps.model_copy(update={"stall_minutes": 7, "max_minutes": 45})
    assert Limits.from_caps(caps) == Limits(
        stall=timedelta(minutes=7), max_time=timedelta(minutes=45)
    )


def test_agent_that_finishes_leaves_the_run_alone(world: World, tmp_path: Path) -> None:
    runner = FakeAgentRunner([[system_init("sess-1"), result("sess-1", 10, 5, "done")]])

    watched = world.watch(runner, _spec(tmp_path))

    assert watched.trip is None
    assert watched.result.outcome == "completed"
    assert load_run(world.folder).state == "building"
    assert load_run(world.folder).pause is None
    assert not [b for b in _bodies(world) if isinstance(b, StateChanged | NeedsYou | Paused)]


def test_agent_that_ignores_the_interrupt_is_cancelled(world: World, tmp_path: Path) -> None:
    class Deaf(FakeAgentRunner):
        async def interrupt(self) -> None:  # the agent never stops on its own
            return None

    runner = Deaf([[system_init("sess-1"), HOLD]])

    watched = world.watch(runner, _spec(tmp_path))

    assert watched.trip is not None and watched.trip.kind == "stall"
    assert watched.result.outcome == "interrupted"
    assert watched.result.session_id == "sess-1"
    assert load_run(world.folder).state == "needs_you"


def test_runner_error_propagates(world: World, tmp_path: Path) -> None:
    runner = FakeAgentRunner([])  # no script: the run raises at once

    with pytest.raises(AgentError):
        world.watch(runner, _spec(tmp_path))
    assert load_run(world.folder).state == "building"


class DropsWhenStopped(FakeAgentRunner):
    """The agent stops when interrupted, then its connection fails (or disconnect raises)."""

    async def run(self, spec: AgentSpec, recorder: SessionRecorder) -> AgentResult:
        await super().run(spec, recorder)
        raise AgentError("the connection to the agent dropped while it was stopping")


def test_error_after_a_stall_still_saves_needs_you(world: World, tmp_path: Path) -> None:
    runner = DropsWhenStopped([[system_init("sess-1"), HOLD]])

    with pytest.raises(AgentError, match="dropped"):
        world.watch(runner, _spec(tmp_path))

    assert load_run(world.folder).state == "needs_you"
    assert world.indexed().state == "needs_you"
    assert len([b for b in _bodies(world) if isinstance(b, NeedsYou)]) == 1


def test_error_after_a_usage_limit_still_saves_the_pause(world: World, tmp_path: Path) -> None:
    reset = START + timedelta(hours=2)
    runner = DropsWhenStopped([[system_init("sess-1"), rate_limit(int(reset.timestamp())), HOLD]])

    with pytest.raises(AgentError, match="dropped"):
        world.watch(runner, _spec(tmp_path, attempt=2))

    saved = load_run(world.folder)
    assert saved.state == "paused"
    assert saved.pause is not None
    assert (saved.pause.reason, saved.pause.resume_at, saved.pause.session_id) == (
        "usage_limit",
        reset,
        "sess-1",
    )
    assert [b for b in _bodies(world) if isinstance(b, Paused)][0].resume_at == reset


# ------------------------------------------------------------- the monitor


def test_activity_resets_the_stall_timer() -> None:
    monitor = Monitor(LIMITS, role="builder", started=START)

    monitor.seen(assistant_text("working"), START + timedelta(minutes=9))

    assert monitor.check(START + timedelta(minutes=18)) is None
    trip = monitor.check(START + timedelta(minutes=19))
    assert trip is not None and trip.kind == "stall"


def test_time_cap_trips_even_with_activity() -> None:
    monitor = Monitor(LIMITS, role="tester", started=START)
    for minute in range(0, 120, 5):
        monitor.seen(assistant_text("still going"), START + timedelta(minutes=minute))

    assert monitor.check(START + timedelta(minutes=119)) is None
    trip = monitor.check(START + timedelta(minutes=120))
    assert trip is not None
    assert trip.kind == "time_cap"
    assert "120 minutes" in trip.detail
    assert trip.resume_at is None


def test_first_trip_wins() -> None:
    monitor = Monitor(LIMITS, role="builder", started=START)
    monitor.seen(rate_limit(int((START + timedelta(hours=1)).timestamp())), START)

    trip = monitor.check(START + timedelta(minutes=500))

    assert trip is not None and trip.kind == "usage_limit"


RESET = START + timedelta(hours=3)


@pytest.mark.parametrize(
    ("message", "resume_at"),
    [
        (rate_limit(int(RESET.timestamp())), RESET),
        (rate_limit(None), START + FALLBACK_RESET),
        (
            assistant_text(
                f"Claude AI usage limit reached|{int(RESET.timestamp())}", error="rate_limit"
            ),
            RESET,
        ),
        (assistant_text("You've hit your limit", error="rate_limit"), START + FALLBACK_RESET),
        (
            result(
                "s", 0, 0, f"Claude AI usage limit reached|{int(RESET.timestamp())}", is_error=True
            ),
            RESET,
        ),
        (result("s", 0, 0, "5-hour limit reached", is_error=True), START + FALLBACK_RESET),
        (
            result("s", 0, 0, "Overloaded", is_error=True, api_error_status=429),
            START + FALLBACK_RESET,
        ),
        # A reset already in the past: resume at once.
        (rate_limit(int((START - timedelta(minutes=5)).timestamp())), START),
    ],
)
def test_usage_limit_signals_and_their_reset_time(message: object, resume_at: datetime) -> None:
    monitor = Monitor(LIMITS, role="builder", started=START)

    monitor.seen(message, START)

    trip = monitor.check(START)
    assert trip is not None
    assert trip.kind == "usage_limit"
    assert trip.resume_at == resume_at


@pytest.mark.parametrize(
    "message",
    [
        rate_limit(int(RESET.timestamp()), status="allowed_warning"),
        rate_limit(int(RESET.timestamp()), status="allowed"),
        rate_limit(int(RESET.timestamp()), overage_status="allowed"),  # carries on in overage
        assistant_text("I hit a limit in the parser", error=None),
        result("s", 0, 0, "tests failed", is_error=True),
        result("s", 0, 0, "Your usage limit is fine", is_error=False),
    ],
)
def test_messages_that_arent_a_usage_limit(message: object) -> None:
    monitor = Monitor(LIMITS, role="builder", started=START)

    monitor.seen(message, START)

    assert monitor.check(START) is None


def test_reset_time_from_an_earlier_rate_limit_event_is_used() -> None:
    monitor = Monitor(LIMITS, role="builder", started=START)
    monitor.seen(rate_limit(int(RESET.timestamp()), status="allowed_warning"), START)

    monitor.seen(assistant_text("You've hit your limit", error="rate_limit"), START)

    trip = monitor.check(START)
    assert trip is not None and trip.resume_at == RESET


def test_turn_cap_result_trips() -> None:
    monitor = Monitor(LIMITS, role="builder", started=START)

    monitor.seen(result("s", 1, 1, is_error=True, subtype="error_max_turns"), START)

    trip = monitor.check(START)
    assert trip is not None
    assert trip.kind == "turn_cap"
    assert trip.resume_at is None


# -------------------------------------------------------------- usage limit


def _limit_turn(reset: datetime) -> list[Step]:
    return [
        system_init("sess-1"),
        assistant_text("Working on it."),
        rate_limit(int(reset.timestamp())),
        assistant_text("Claude AI usage limit reached", error="rate_limit"),
        result("sess-1", 5, 2, "Claude AI usage limit reached", is_error=True),
    ]


def test_usage_limit_pauses_with_a_resume_time(world: World, tmp_path: Path) -> None:  # AC2
    reset = START + timedelta(hours=2)
    runner = FakeAgentRunner([_limit_turn(reset)])

    watched = world.watch(runner, _spec(tmp_path, attempt=2))

    assert watched.trip is not None and watched.trip.kind == "usage_limit"
    saved = load_run(world.folder)
    assert saved.state == "paused"
    assert saved.pause == Pause(
        reason="usage_limit",
        detail=watched.trip.detail,
        state="building",
        role="builder",
        attempt=2,
        session_id="sess-1",
        resume_at=reset,
    )
    assert world.indexed() == saved
    bodies = _bodies(world)
    assert StateChanged(from_state="building", to_state="paused") in bodies
    paused = [b for b in bodies if isinstance(b, Paused)]
    assert paused == [Paused(reason="usage_limit", detail=watched.trip.detail, resume_at=reset)]


class CountsInterrupts(FakeAgentRunner):
    def __init__(self, turns: list[list[Step]]) -> None:
        super().__init__(turns)
        self.interrupts = 0

    async def interrupt(self) -> None:
        self.interrupts += 1
        await super().interrupt()


@pytest.mark.parametrize(
    "limit_message",
    [
        pytest.param(lambda reset: rate_limit(int(reset.timestamp())), id="rate-limit-event"),
        pytest.param(
            lambda reset: assistant_text(
                f"Claude AI usage limit reached|{int(reset.timestamp())}", error="rate_limit"
            ),
            id="rate-limit-error",
        ),
    ],
)
def test_a_usage_limit_mid_turn_interrupts_the_agent(
    world: World, tmp_path: Path, limit_message: Callable[[datetime], Step]
) -> None:
    """The agent would wait forever after the limit message (HOLD): only the watchdog's
    interrupt ends its turn, well before the stall limit, and the pause has the CLI's reset."""
    reset = START + timedelta(hours=2)
    runner = CountsInterrupts([[system_init("sess-1"), limit_message(reset), HOLD]])

    watched = world.watch(runner, _spec(tmp_path))

    assert runner.interrupts == 1
    assert watched.result.outcome == "interrupted"
    assert world.time.now - START < timedelta(minutes=1)
    assert watched.trip is not None and watched.trip.kind == "usage_limit"
    saved = load_run(world.folder)
    assert saved.state == "paused"
    assert saved.pause is not None and saved.pause.resume_at == reset


def test_after_the_reset_the_same_session_resumes(world: World, tmp_path: Path) -> None:  # AC3
    reset = START + timedelta(hours=2)
    runner = FakeAgentRunner(
        [_limit_turn(reset), [system_init("sess-1"), result("sess-1", 3, 3, "done")]]
    )
    spec = _spec(tmp_path, attempt=2)
    world.watch(runner, spec)
    pause = world.run.pause
    assert pause is not None

    asyncio.run(wait_for_reset(world.run, clock=world.time.clock, sleep=world.time.sleep))
    assert world.time.now >= reset
    world.run = unpause(
        world.run,
        now=world.time.now,
        events=world.events,
        run_folder=world.folder,
        registry=world.registry,
    )
    watched = world.watch(runner, resume_spec(spec, pause))

    assert runner.specs[-1].resume == "sess-1"
    assert runner.specs[-1].attempt == 2
    assert runner.specs[-1].prompt == RESUME_PROMPT
    assert watched.trip is None
    assert watched.result.outcome == "completed"
    saved = load_run(world.folder)
    assert saved.state == "building"
    assert saved.pause is None
    assert saved.session_ids == {"builder": "sess-1"}
    assert StateChanged(from_state="paused", to_state="building") in _bodies(world)


def test_wait_for_reset_sleeps_until_the_reset(world: World, tmp_path: Path) -> None:
    reset = START + timedelta(hours=2, seconds=30)
    world.watch(FakeAgentRunner([_limit_turn(reset)]), _spec(tmp_path))

    asyncio.run(wait_for_reset(world.run, clock=world.time.clock, sleep=world.time.sleep))

    assert world.time.now == reset


def test_unpause_before_the_reset_raises(world: World, tmp_path: Path) -> None:
    reset = START + timedelta(hours=2)
    world.watch(FakeAgentRunner([_limit_turn(reset)]), _spec(tmp_path))

    with pytest.raises(AgentError) as exc:
        unpause(
            world.run,
            now=reset - timedelta(seconds=1),
            events=world.events,
            run_folder=world.folder,
            registry=world.registry,
        )
    assert "usage limit" in str(exc.value)
    assert load_run(world.folder).state == "paused"


def test_unpause_needs_a_paused_run(world: World) -> None:
    with pytest.raises(AgentError):
        unpause(
            world.run,
            now=START,
            events=world.events,
            run_folder=world.folder,
            registry=world.registry,
        )


def test_wait_for_reset_needs_a_reset_time(world: World) -> None:
    with pytest.raises(AgentError):
        asyncio.run(wait_for_reset(world.run, clock=world.time.clock, sleep=world.time.sleep))


# --------------------------------------------------------------------- caps


def test_turn_cap_pauses_the_run_with_the_reason(world: World, tmp_path: Path) -> None:  # AC4
    runner = FakeAgentRunner(
        [[system_init("sess-1"), result("sess-1", 9, 9, is_error=True, subtype="error_max_turns")]]
    )

    watched = world.watch(runner, _spec(tmp_path))

    assert watched.trip is not None and watched.trip.kind == "turn_cap"
    saved = load_run(world.folder)
    assert saved.state == "paused"
    assert saved.pause is not None
    assert saved.pause.reason == "turn_cap"
    assert saved.pause.resume_at is None
    assert "200 turns" in saved.pause.detail
    paused = [b for b in _bodies(world) if isinstance(b, Paused)]
    assert paused == [Paused(reason="turn_cap", detail=saved.pause.detail, resume_at=None)]


def test_time_cap_stops_the_agent_and_pauses(world: World, tmp_path: Path) -> None:  # AC4
    runner = FakeAgentRunner([[system_init("sess-1"), HOLD]])
    limits = Limits(stall=timedelta(minutes=60), max_time=timedelta(minutes=30))

    watched = world.watch(runner, _spec(tmp_path), limits)

    assert watched.trip is not None and watched.trip.kind == "time_cap"
    assert watched.result.outcome == "interrupted"
    assert timedelta(minutes=30) <= world.time.now - START < timedelta(minutes=31)
    saved = load_run(world.folder)
    assert saved.state == "paused"
    assert saved.pause is not None
    assert saved.pause.reason == "time_cap"
    assert "30 minutes" in saved.pause.detail


def test_paused_by_a_cap_can_be_resumed_when_you_say_so(world: World, tmp_path: Path) -> None:
    runner = FakeAgentRunner(
        [[system_init("sess-1"), result("sess-1", 9, 9, is_error=True, subtype="error_max_turns")]]
    )
    world.watch(runner, _spec(tmp_path))

    run = unpause(
        world.run,
        now=world.time.now,
        events=world.events,
        run_folder=world.folder,
        registry=world.registry,
    )

    assert run.state == "building"
    assert run.pause is None
