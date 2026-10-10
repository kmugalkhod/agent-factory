"""The per-run event stream: an append-only `events.jsonl` in the run's folder, and its reader.

Each line is one JSON object: a header (`seq`, `at`, `repo`, `run_id`, `role`) and a typed
`body` whose `type` names the event. A line counts only once its newline is written, so a
crash mid-write leaves an unfinished last line that readers ignore and the next writer cuts
off before appending. Sequence numbers start at 1 and strictly increase per run.

One `EventLog` writes each run; the engine runs one writer per run.
"""

import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from factory_engine.errors import EventLogError
from factory_engine.run import NonNegative, Role, RunState

EVENTS_FILE = "events.jsonl"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class StateChanged(_Model):
    type: Literal["state_changed"] = "state_changed"
    from_state: RunState
    to_state: RunState


class StageStarted(_Model):
    type: Literal["stage_started"] = "stage_started"
    stage: Role
    attempt: Annotated[int, Field(ge=1)]


class StageFinished(_Model):
    type: Literal["stage_finished"] = "stage_finished"
    stage: Role
    attempt: Annotated[int, Field(ge=1)]
    outcome: Literal["completed", "failed", "interrupted"]


class ToolCall(_Model):
    """One safety-hook decision: the tool, what it targeted, allowed or blocked, and why."""

    type: Literal["tool_call"] = "tool_call"
    tool: str
    target: str | None
    decision: Literal["allowed", "blocked"]
    reason: str | None


class SkillLoaded(_Model):
    type: Literal["skill_loaded"] = "skill_loaded"
    skill: str


class GateResult(_Model):
    type: Literal["gate_result"] = "gate_result"
    gate: str
    passed: bool
    reason: str


class TestRun(_Model):
    type: Literal["test_run"] = "test_run"
    command: str
    exit_code: int
    passed: NonNegative | None
    failed: NonNegative | None
    seconds: Annotated[float, Field(ge=0)]


class Tokens(_Model):
    type: Literal["tokens"] = "tokens"
    attempt: Annotated[int, Field(ge=1)]
    input: NonNegative
    output: NonNegative
    cache_read: NonNegative = 0
    cache_creation: NonNegative = 0


class Message(_Model):
    """A message to or from an agent mid-run: `you` wrote it, or the `agent` replied."""

    type: Literal["message"] = "message"
    sender: Literal["you", "agent"]
    text: str


class NeedsYou(_Model):
    type: Literal["needs_you"] = "needs_you"
    reason: str


Body = Annotated[
    StateChanged
    | StageStarted
    | StageFinished
    | ToolCall
    | SkillLoaded
    | GateResult
    | TestRun
    | Tokens
    | Message
    | NeedsYou,
    Field(discriminator="type"),
]


class Event(_Model):
    seq: Annotated[int, Field(ge=1)]
    at: AwareDatetime
    repo: str
    run_id: NonNegative
    role: Role | None
    body: Body


class EventLog:
    """Appends events to `<folder>/events.jsonl`. Safe to share between threads."""

    def __init__(
        self, folder: Path, *, repo: str, run_id: int, clock: Callable[[], datetime]
    ) -> None:
        self._folder = folder
        self.path = folder / EVENTS_FILE
        self._repo = repo
        self._run_id = run_id
        self._clock = clock
        self._lock = threading.Lock()
        self._last_seq = 0
        self._recover()

    @property
    def repo(self) -> str:
        return self._repo

    @property
    def run_id(self) -> int:
        return self._run_id

    def append(self, body: Body, *, role: Role | None = None) -> Event:
        with self._lock:
            if self._needs_recovery:
                self._recover()
            event = Event(
                seq=self._last_seq + 1,
                at=self._clock(),
                repo=self._repo,
                run_id=self._run_id,
                role=role,
                body=body,
            )
            line = (event.model_dump_json() + "\n").encode("utf-8")
            try:
                with self.path.open("ab") as handle:
                    handle.write(line)
                    handle.flush()
            except OSError as err:
                # Part or all of the line may be on disk: recover before the next append.
                self._needs_recovery = True
                raise EventLogError(
                    f"{self.path}: can't append event {event.seq} ({err}). "
                    "Check the run folder is writable."
                ) from err
            self._last_seq = event.seq
            return event

    def _recover(self) -> None:
        """Cut an unfinished last line and take the sequence from the file, which is the
        truth after a crash or a failed write. Stays marked if it fails, so no append runs
        on a log in an unknown state."""
        self._needs_recovery = True
        self._cut_unfinished_line()
        events = read_events(self._folder)
        self._last_seq = events[-1].seq if events else 0
        self._needs_recovery = False

    def _cut_unfinished_line(self) -> None:
        """Drop bytes after the last newline: a line a crash left unfinished."""
        try:
            with self.path.open("r+b") as handle:
                data = handle.read()
                keep = data.rfind(b"\n") + 1
                if keep < len(data):
                    handle.truncate(keep)
        except FileNotFoundError:
            return
        except OSError as err:
            raise EventLogError(f"{self.path}: can't open the event log ({err}).") from err


def read_events(folder: Path, after: int = 0) -> list[Event]:
    """Every complete event in `<folder>/events.jsonl` with `seq > after`, in order.

    An unfinished last line (no newline yet) is ignored. A complete line that isn't a valid
    event, or breaks the sequence order, raises EventLogError naming the file and line.
    """
    path = folder / EVENTS_FILE
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError as err:
        raise EventLogError(f"{path}: can't read the event log ({err}).") from err
    events, _, _ = _parse(path, _complete(data), lines_before=0, last_seq=0)
    return [event for event in events if event.seq > after]


class EventTail:
    """Follows one run's events for a live view: each `poll` reads only bytes it hasn't seen.

    The first poll reads the file from the start; later polls continue from where the last
    one stopped, so a poll with nothing new costs one small read. Lines already passed are not
    checked again; `read_events` checks the whole file.
    """

    def __init__(self, folder: Path, after: int = 0) -> None:
        self.path = folder / EVENTS_FILE
        self._after = after
        self._offset = 0
        self._lines = 0
        self._last_seq = 0

    def poll(self) -> list[Event]:
        """New complete events since the last poll, with `seq > after`."""
        try:
            with self.path.open("rb") as handle:
                handle.seek(self._offset)
                data = handle.read()
        except FileNotFoundError:
            return []
        except OSError as err:
            raise EventLogError(f"{self.path}: can't read the event log ({err}).") from err
        complete = _complete(data)
        events, self._last_seq, self._lines = _parse(
            self.path, complete, lines_before=self._lines, last_seq=self._last_seq
        )
        self._offset += len(complete)
        return [event for event in events if event.seq > self._after]


def _complete(data: bytes) -> bytes:
    """The bytes up to and including the last newline: whole lines only."""
    return data[: data.rfind(b"\n") + 1]


def _parse(
    path: Path, complete: bytes, *, lines_before: int, last_seq: int
) -> tuple[list[Event], int, int]:
    """Parse whole lines; returns the events, the last seq and the total line count."""
    events: list[Event] = []
    number = lines_before
    for raw in complete.splitlines():
        number += 1
        try:
            event = Event.model_validate_json(raw)
        except ValidationError as err:
            raise EventLogError(
                f"{path}: line {number} is not a valid event ({_problems(err)})."
            ) from err
        if event.seq <= last_seq:
            raise EventLogError(
                f"{path}: line {number} has seq {event.seq} after {last_seq}; "
                "sequence numbers must strictly increase."
            )
        last_seq = event.seq
        events.append(event)
    return events, last_seq, number


def _problems(err: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in item['loc']) or '<root>'}: {item['msg']}"
        for item in err.errors()
    )
