"""The watchdog: stop a stalled agent, pause on the usage limit, and keep to the turn and time
caps (design: failure handling, limits and quality).

- `Monitor` decides from the agent's messages and the time alone. Any message from the agent
  counts as activity (its tool calls and their results arrive as messages); none for the stall
  limit stops it. A rejected rate-limit update, a `rate_limit` error or an error result that
  reports the usage limit means the subscription's limit is hit; the reset time comes from the
  CLI when it gives one. A `max_turns` result is the turn cap, and the time cap counts from the
  agent's start.
- `Watchdog.run` runs one agent under a monitor. On a trip it stops the agent (cancelling it if
  it doesn't stop within a grace period), then moves the run: a stall to needs_you, a usage
  limit or a cap to paused, with a `Pause` that records where it stopped.
- `wait_for_reset`, `unpause` and `resume_spec` carry a paused run on in the same session: a
  usage limit after its reset time, a cap when you say so.
"""

import asyncio
import contextlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from claude_agent_sdk import AssistantMessage, RateLimitEvent, ResultMessage, TextBlock

from factory_engine.agents.runner import (
    AgentResult,
    AgentRunner,
    AgentSpec,
    RunIndex,
    SessionRecorder,
    persist,
)
from factory_engine.config import Caps
from factory_engine.errors import AgentError
from factory_engine.events import EventLog, NeedsYou, Paused
from factory_engine.run import Pause, PauseReason, Role, Run
from factory_engine.states import move

TripKind = Literal["stall", "usage_limit", "turn_cap", "time_cap"]

POLL = timedelta(seconds=5)  # how often the watchdog looks at the clock
GRACE = timedelta(seconds=30)  # how long a stopped agent gets to finish its turn
# When the CLI says the limit is hit but not when it resets. Resuming too early only pauses
# again, so wait an hour rather than the whole five-hour window.
FALLBACK_RESET = timedelta(hours=1)
RESUME_PROMPT = "The run was paused. Carry on with your task from where you stopped."

LIMIT_TEXT = re.compile(r"usage limit|hit your limit|limit reached", re.IGNORECASE)
RESET_EPOCH = re.compile(r"\|(\d{9,11})\b")  # "Claude AI usage limit reached|<unix seconds>"
TOO_MANY_REQUESTS = 429
GOING_ON: frozenset[str] = frozenset({"allowed", "allowed_warning"})

type Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class Limits:
    stall: timedelta
    max_time: timedelta

    @classmethod
    def from_caps(cls, caps: Caps) -> "Limits":
        return cls(
            stall=timedelta(minutes=caps.stall_minutes),
            max_time=timedelta(minutes=caps.max_minutes),
        )


@dataclass(frozen=True)
class Trip:
    kind: TripKind
    detail: str  # what happened, for you
    resume_at: datetime | None = None  # usage limit only


@dataclass(frozen=True)
class Watched:
    result: AgentResult  # its `run` is the run as last saved, moved if the watchdog tripped
    trip: Trip | None


class Monitor:
    """Watches one agent run. Feed it every message with `seen`; `check` says whether to stop."""

    def __init__(
        self, limits: Limits, *, role: Role, started: datetime, max_turns: int | None = None
    ) -> None:
        self._limits = limits
        self._role = role
        self._started = started
        self._last = started
        self._max_turns = max_turns
        self._known_reset: datetime | None = None  # from the CLI's latest rate-limit update
        self._trip: Trip | None = None
        self._guessed_reset = False

    @property
    def trip(self) -> Trip | None:
        return self._trip

    def seen(self, message: object, now: datetime) -> None:
        self._last = max(self._last, now)
        if isinstance(message, RateLimitEvent):
            info = message.rate_limit_info
            if info.resets_at is not None:
                self._known_reset = _from_unix(info.resets_at)
            if info.status == "rejected" and info.overage_status not in GOING_ON:
                self._usage_limit(now, None)
        elif isinstance(message, AssistantMessage):
            if message.error == "rate_limit":
                text = "\n".join(b.text for b in message.content if isinstance(b, TextBlock))
                self._usage_limit(now, _text_reset(text))
        elif isinstance(message, ResultMessage):
            if message.subtype == "error_max_turns" or message.terminal_reason == "max_turns":
                turns = f" ({self._max_turns} turns)" if self._max_turns is not None else ""
                self._set(Trip("turn_cap", f"the {self._role} reached its turn cap{turns}."))
            elif message.is_error and (
                message.api_error_status == TOO_MANY_REQUESTS
                or LIMIT_TEXT.search(message.result or "")
            ):
                self._usage_limit(now, _text_reset(message.result or ""))

    def check(self, now: datetime) -> Trip | None:
        """The trip, if the agent should be stopped at `now`."""
        if self._trip is None:
            if now - self._started >= self._limits.max_time:
                self._trip = Trip(
                    "time_cap",
                    f"the {self._role} reached its time cap "
                    f"({_minutes(self._limits.max_time)} minutes).",
                )
            elif now - self._last >= self._limits.stall:
                self._trip = Trip(
                    "stall",
                    f"no tool activity from the {self._role} for "
                    f"{_minutes(self._limits.stall)} minutes; the agent was stopped.",
                )
        return self._trip

    def _usage_limit(self, now: datetime, reset: datetime | None) -> None:
        reset = reset or self._known_reset
        if self._trip is not None:
            # The first trip stands; only a guessed reset time gives way to the CLI's own.
            improves = self._trip.kind == "usage_limit" and self._guessed_reset
            if not (improves and reset is not None):
                return
        self._guessed_reset = reset is None
        resume_at = max(reset or now + FALLBACK_RESET, now)
        self._trip = Trip(
            "usage_limit",
            f"the subscription's usage limit was reached during the {self._role} stage; "
            f"the run resumes at {resume_at:%Y-%m-%d %H:%M %Z}.",
            resume_at,
        )

    def _set(self, trip: Trip) -> None:
        if self._trip is None:
            self._trip = trip


def _from_unix(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, tz=UTC)


def _text_reset(text: str) -> datetime | None:
    found = RESET_EPOCH.search(text)
    return _from_unix(int(found.group(1))) if found else None


def _minutes(span: timedelta) -> int:
    return int(span.total_seconds() // 60)


class Watchdog:
    """Runs agents under a `Monitor`. `clock` and `sleep` are passed in so tests can fake time."""

    def __init__(
        self,
        limits: Limits,
        *,
        clock: Callable[[], datetime],
        sleep: Sleep = asyncio.sleep,
        poll: timedelta = POLL,
        grace: timedelta = GRACE,
    ) -> None:
        self._limits = limits
        self._clock = clock
        self._sleep = sleep
        self._poll = poll
        self._grace = grace

    async def run(
        self, runner: AgentRunner, spec: AgentSpec, recorder: SessionRecorder, *, events: EventLog
    ) -> Watched:
        """Run `spec`'s agent through `runner`, stopping it on a stall, usage limit or cap and
        moving the run to match. `events` is the run's log (the recorder's own)."""
        monitor = Monitor(
            self._limits, role=spec.role, started=self._clock(), max_turns=spec.max_turns
        )
        recorder.listen(lambda message: monitor.seen(message, self._clock()))
        agent = asyncio.ensure_future(runner.run(spec, recorder))
        stopper: asyncio.Future[None] | None = None
        cancelled = False  # by the watchdog, after the grace ran out
        try:
            deadline: datetime | None = None  # when a stopped agent's grace runs out
            while not agent.done():
                sleeper = asyncio.ensure_future(self._sleep(self._poll.total_seconds()))
                try:
                    await asyncio.wait({agent, sleeper}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    sleeper.cancel()
                if agent.done():
                    break
                now = self._clock()
                if deadline is None:
                    if monitor.check(now) is not None:
                        deadline = now + self._grace
                        # In the background: an agent that doesn't answer is cancelled below.
                        stopper = asyncio.ensure_future(runner.interrupt())
                elif now >= deadline:
                    agent.cancel()
                    cancelled = True
                    break
            try:
                result = await agent
            except asyncio.CancelledError:
                if not cancelled:
                    raise  # cancelled from outside, not by the watchdog
                result = recorder.finish(interrupted=True)
        finally:
            if not agent.done():
                agent.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await agent
            if stopper is not None:
                stopper.cancel()
                # Not running any more (AgentError) or a dropped connection: the agent is
                # stopped either way.
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await stopper
        trip = monitor.trip
        if trip is not None:
            result = replace(result, run=_settle(recorder, spec, trip, events))
        return Watched(result, trip)


def _settle(recorder: SessionRecorder, spec: AgentSpec, trip: Trip, events: EventLog) -> Run:
    """Move the run for `trip` and log why: a stall needs you, anything else pauses."""
    run = recorder.run
    if trip.kind == "stall":
        moved = move(run, "needs_you", log=events)
        events.append(NeedsYou(reason=trip.detail), role=spec.role)
    else:
        reason: PauseReason = trip.kind
        pause = Pause(
            reason=reason,
            detail=trip.detail,
            state=run.state,
            role=spec.role,
            attempt=spec.attempt,
            session_id=recorder.session_id,
            resume_at=trip.resume_at,
        )
        moved = move(run, "paused", log=events).model_copy(update={"pause": pause})
        events.append(
            Paused(reason=reason, detail=trip.detail, resume_at=trip.resume_at), role=spec.role
        )
    recorder.save(moved)
    return moved


# ------------------------------------------------------------------ resuming


def _paused(run: Run) -> Pause:
    if run.state != "paused" or run.pause is None:
        raise AgentError(
            f"run {run.repo}/{run.id} isn't paused (it is {run.state}); there is nothing to resume."
        )
    return run.pause


async def wait_for_reset(run: Run, *, clock: Callable[[], datetime], sleep: Sleep) -> None:
    """Sleep until a run paused by the usage limit may resume."""
    pause = _paused(run)
    if pause.resume_at is None:
        raise AgentError(
            f"run {run.repo}/{run.id} was paused for its {pause.reason}, which has no reset "
            "time. Resume it when you decide to."
        )
    while (left := (pause.resume_at - clock()).total_seconds()) > 0:
        await sleep(left)


def unpause(
    run: Run, *, now: datetime, events: EventLog, run_folder: Path, registry: RunIndex
) -> Run:
    """Move a paused run back to the stage it stopped in and drop its pause record. A run
    paused by the usage limit can't resume before its reset time."""
    pause = _paused(run)
    if pause.resume_at is not None and now < pause.resume_at:
        raise AgentError(
            f"run {run.repo}/{run.id} is paused by the usage limit until "
            f"{pause.resume_at:%Y-%m-%d %H:%M %Z}. Wait for the reset before resuming."
        )
    moved = move(run, pause.state, log=events).model_copy(update={"pause": None})
    persist(moved, run_folder, registry)
    return moved


def resume_spec(spec: AgentSpec, pause: Pause) -> AgentSpec:
    """`spec` changed to carry on the paused agent's session, in the same attempt."""
    if pause.role != spec.role:
        raise AgentError(
            f"the run paused in the {pause.role} stage; a {spec.role} spec can't resume it."
        )
    if pause.session_id is None:
        raise AgentError(
            f"the paused {pause.role} has no saved session to resume. Start a fresh session."
        )
    return replace(spec, resume=pause.session_id, attempt=pause.attempt, prompt=RESUME_PROMPT)
