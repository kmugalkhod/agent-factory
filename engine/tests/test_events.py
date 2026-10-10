"""Tests for factory_engine.events (task 2.4): the per-run events.jsonl log and its reader."""

import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from factory_engine import events
from factory_engine.errors import EventLogError
from factory_engine.events import (
    EVENTS_FILE,
    Event,
    EventLog,
    GateResult,
    Message,
    NeedsYou,
    SkillLoaded,
    StageFinished,
    StageStarted,
    StateChanged,
    Tokens,
    ToolCall,
    read_events,
)

START = datetime(2026, 10, 10, 9, 30, tzinfo=UTC)

ONE_OF_EACH = [
    (StateChanged(from_state="pending", to_state="planning"), None),
    (StageStarted(stage="planner", attempt=1), "planner"),
    (ToolCall(tool="Write", target="plan.md", decision="allowed", reason=None), "planner"),
    (ToolCall(tool="Bash", target="git push", decision="blocked", reason="git"), "builder"),
    (SkillLoaded(skill="write-plan"), "planner"),
    (StageFinished(stage="planner", attempt=1, outcome="completed"), "planner"),
    (GateResult(gate="plan", passed=False, reason="no acceptance criteria"), None),
    (
        events.TestRun(command="uv run pytest", exit_code=1, passed=3, failed=2, seconds=4.5),
        "tester",
    ),
    (Tokens(attempt=1, input=1200, output=300, cache_read=50_000, cache_creation=900), "tester"),
    (Message(sender="you", text="Use the existing helper."), "builder"),
    (NeedsYou(reason="builder cap reached"), None),
]


def _clock() -> Iterator[datetime]:
    moment = START
    while True:
        yield moment
        moment += timedelta(seconds=1)


def _log(folder: Path) -> EventLog:
    ticks = _clock()
    return EventLog(folder, repo="app", run_id=3, clock=lambda: next(ticks))


def test_every_event_type_round_trips(tmp_path: Path) -> None:
    log = _log(tmp_path)
    written = [log.append(body, role=role) for body, role in ONE_OF_EACH]
    assert read_events(tmp_path) == written
    assert [e.body for e in written] == [body for body, _ in ONE_OF_EACH]


def test_every_event_has_a_timestamp_run_id_and_role(tmp_path: Path) -> None:  # AC4
    log = _log(tmp_path)
    for body, role in ONE_OF_EACH:
        log.append(body, role=role)
    lines = (tmp_path / EVENTS_FILE).read_text(encoding="utf-8").splitlines()
    for line, (_, role) in zip(lines, ONE_OF_EACH, strict=True):
        data = json.loads(line)
        assert datetime.fromisoformat(data["at"]).tzinfo is not None
        assert data["repo"] == "app"
        assert data["run_id"] == 3
        assert "role" in data and data["role"] == role


def test_sequence_numbers_start_at_1_and_strictly_increase(tmp_path: Path) -> None:  # AC1
    log = _log(tmp_path)
    for body, role in ONE_OF_EACH:
        log.append(body, role=role)
    assert [e.seq for e in read_events(tmp_path)] == list(range(1, len(ONE_OF_EACH) + 1))


def test_a_new_writer_continues_the_sequence(tmp_path: Path) -> None:  # AC1
    _log(tmp_path).append(NeedsYou(reason="first"))
    _log(tmp_path).append(NeedsYou(reason="second"))
    assert [e.seq for e in read_events(tmp_path)] == [1, 2]


def test_concurrent_appends_get_unique_increasing_numbers(tmp_path: Path) -> None:  # AC1
    log = _log(tmp_path)

    def burst(n: int) -> None:
        for i in range(25):
            log.append(Message(sender="agent", text=f"{n}-{i}"))

    threads = [threading.Thread(target=burst, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert [e.seq for e in read_events(tmp_path)] == list(range(1, 101))


def test_a_partly_written_last_line_is_ignored_on_read(tmp_path: Path) -> None:  # AC2
    log = _log(tmp_path)
    for body, role in ONE_OF_EACH[:3]:
        log.append(body, role=role)
    with (tmp_path / EVENTS_FILE).open("a", encoding="utf-8") as handle:
        handle.write('{"seq": 4, "at": "2026-10-1')  # crash mid-write
    assert [e.seq for e in read_events(tmp_path)] == [1, 2, 3]


def test_a_partly_written_last_line_doesnt_break_later_appends(tmp_path: Path) -> None:  # AC2
    log = _log(tmp_path)
    for body, role in ONE_OF_EACH[:3]:
        log.append(body, role=role)
    with (tmp_path / EVENTS_FILE).open("a", encoding="utf-8") as handle:
        handle.write('{"seq": 4, "at": "2026-10-1')
    after_crash = _log(tmp_path)
    event = after_crash.append(NeedsYou(reason="after the crash"))
    assert event.seq == 4
    assert [e.seq for e in read_events(tmp_path)] == [1, 2, 3, 4]
    assert read_events(tmp_path)[-1].body == NeedsYou(reason="after the crash")


def test_a_complete_json_line_without_newline_counts_as_unfinished(tmp_path: Path) -> None:
    log = _log(tmp_path)
    first = log.append(NeedsYou(reason="kept"))
    line = first.model_dump_json().replace('"seq":1', '"seq":2')
    with (tmp_path / EVENTS_FILE).open("a", encoding="utf-8") as handle:
        handle.write(line)  # the newline never made it
    assert read_events(tmp_path) == [first]


def test_reader_returns_only_events_after_a_sequence_number(tmp_path: Path) -> None:  # AC3
    log = _log(tmp_path)
    for body, role in ONE_OF_EACH:
        log.append(body, role=role)
    assert [e.seq for e in read_events(tmp_path, after=7)] == [8, 9, 10, 11]
    assert read_events(tmp_path, after=11) == []
    assert len(read_events(tmp_path, after=0)) == 11


def test_reader_picks_up_new_events_on_the_next_call(tmp_path: Path) -> None:  # AC3: tailing
    log = _log(tmp_path)
    log.append(NeedsYou(reason="one"))
    seen = read_events(tmp_path)
    log.append(NeedsYou(reason="two"))
    assert [e.seq for e in read_events(tmp_path, after=seen[-1].seq)] == [2]


def test_reading_a_run_without_events_gives_nothing(tmp_path: Path) -> None:
    assert read_events(tmp_path) == []


@pytest.mark.parametrize(
    "line",
    [
        '{"not json"',
        '{"seq": 1, "at": "2026-10-10T09:30:00+00:00", "repo": "app", "run_id": 3, '
        '"role": null, "body": {"type": "teleported"}}',
        '{"seq": 1, "at": "2026-10-10T09:30:00", "repo": "app", "run_id": 3, '
        '"role": null, "body": {"type": "needs_you", "reason": "x"}}',
        '{"seq": 1, "at": "2026-10-10T09:30:00+00:00", "repo": "app", "run_id": 3, '
        '"body": {"type": "needs_you", "reason": "x"}}',
    ],
    ids=["broken-json", "unknown-type", "naive-timestamp", "missing-role"],
)
def test_a_complete_invalid_line_names_the_file_and_line(tmp_path: Path, line: str) -> None:
    _log(tmp_path).append(NeedsYou(reason="fine"))
    with (tmp_path / EVENTS_FILE).open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    with pytest.raises(EventLogError) as exc:
        read_events(tmp_path)
    assert str(tmp_path / EVENTS_FILE) in str(exc.value)
    assert "line 2" in str(exc.value)


def test_out_of_order_sequence_numbers_are_refused(tmp_path: Path) -> None:  # AC1
    log = _log(tmp_path)
    first = log.append(NeedsYou(reason="one"))
    with (tmp_path / EVENTS_FILE).open("a", encoding="utf-8") as handle:
        handle.write(first.model_dump_json() + "\n")  # seq 1 again
    with pytest.raises(EventLogError) as exc:
        read_events(tmp_path)
    assert "line 2" in str(exc.value)


def test_event_bodies_are_validated() -> None:
    with pytest.raises(ValidationError):
        StateChanged(from_state="pending", to_state="dancing")  # type: ignore[arg-type]  # bad state on purpose
    with pytest.raises(ValidationError):
        events.TestRun(command="pytest", exit_code=0, passed=-1, failed=0, seconds=1.0)


def test_a_naive_clock_is_refused(tmp_path: Path) -> None:
    log = EventLog(tmp_path, repo="app", run_id=3, clock=lambda: datetime(2026, 10, 10))
    with pytest.raises(ValidationError):
        log.append(NeedsYou(reason="x"))


def test_event_model_holds_the_header_fields() -> None:
    event = Event(
        seq=1, at=START, repo="app", run_id=3, role="builder", body=NeedsYou(reason="cap")
    )
    assert (event.seq, event.at, event.run_id, event.role) == (1, START, 3, "builder")
