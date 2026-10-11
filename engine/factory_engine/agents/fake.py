"""`FakeAgentRunner`: replays scripted SDK messages instead of running a model, for tests.

A script is a list of turns: the first turn plays when the agent runs, and each `send` plays
the next one. A turn is a list of SDK messages (build them with `system_init`,
`assistant_text` and `result`), recorded through the same `SessionRecorder` the real runner
uses. `HOLD` in a turn waits there until the test calls `interrupt` or `send`, which end the
turn; `started` is set when a turn reaches it.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock

from factory_engine.agents.runner import (
    AgentResult,
    AgentSpec,
    SessionRecorder,
    check_pairing,
    not_running,
)
from factory_engine.errors import AgentError

HOLD: Final = "hold"
FAKE_MODEL = "fake-model"

type Step = SystemMessage | AssistantMessage | ResultMessage | str


def system_init(session_id: str) -> SystemMessage:
    return SystemMessage(subtype="init", data={"type": "system", "session_id": session_id})


def assistant_text(text: str, session_id: str | None = None) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model=FAKE_MODEL, session_id=session_id)


def result(
    session_id: str,
    input_tokens: int,
    output_tokens: int,
    text: str | None = None,
    *,
    cache_read: int = 0,
    cache_creation: int = 0,
    is_error: bool = False,
) -> ResultMessage:
    return ResultMessage(
        subtype="error_during_execution" if is_error else "success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id=session_id,
        usage={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
        },
        result=text,
    )


@dataclass
class _Active:
    recorder: SessionRecorder
    turns: int = 1
    stop: bool = False
    interrupted: bool = False


class FakeAgentRunner:
    """An `AgentRunner` that plays `turns` in order, one turn per run or `send`."""

    def __init__(self, turns: Sequence[Sequence[Step]]) -> None:
        self._turns = [list(turn) for turn in turns]
        self._next = 0
        self._active: _Active | None = None
        self._wake = asyncio.Event()
        self.started = asyncio.Event()
        self.specs: list[AgentSpec] = []
        self.sent: list[str] = []

    async def run(self, spec: AgentSpec, recorder: SessionRecorder) -> AgentResult:
        if self._active is not None:
            raise AgentError(
                "an agent is already running on this runner; use one runner per agent."
            )
        check_pairing(spec, recorder)
        self.specs.append(spec)
        active = self._active = _Active(recorder)
        try:
            played = 0
            while played < active.turns:
                await self._play(active, self._take_turn())
                played += 1
        finally:
            self._active = None
        return recorder.finish(interrupted=active.interrupted)

    async def interrupt(self) -> None:
        active = self._active
        if active is None:
            raise not_running()
        active.interrupted = True
        self._stop(active)

    async def send(self, text: str) -> None:
        active = self._active
        if active is None:
            raise not_running()
        active.turns += 1
        active.interrupted = False
        active.recorder.sent(text)
        self.sent.append(text)
        self._stop(active)

    def _stop(self, active: _Active) -> None:
        active.stop = True
        self._wake.set()

    def _take_turn(self) -> list[Step]:
        if self._next >= len(self._turns):
            raise AgentError(
                f"the fake agent's script has {len(self._turns)} turns; turn "
                f"{self._next + 1} was asked for. Add a turn to the script."
            )
        turn = self._turns[self._next]
        self._next += 1
        return turn

    async def _play(self, active: _Active, turn: list[Step]) -> None:
        active.stop = False
        self._wake.clear()
        for step in turn:
            if step == HOLD:
                self.started.set()
                await self._wake.wait()
            else:
                await asyncio.sleep(0)  # let the test interleave, as a real stream would
            if active.stop:
                return
            if not isinstance(step, str):
                active.recorder.record(step)
