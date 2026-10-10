"""Tests for factory_engine.states (task 2.5): the run state machine.

The legal transitions are written out here by hand, independently of states.py, so the
exhaustive test checks the code against the agreed table, not against itself.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from factory_engine.errors import EventLogError, IllegalTransitionError
from factory_engine.events import EventLog, StateChanged, read_events
from factory_engine.run import Run, RunState, load_run, new_run, save_run
from factory_engine.states import TRANSITIONS, move, transition

NOW = datetime(2026, 10, 10, 9, 30, tzinfo=UTC)
LATER = NOW + timedelta(minutes=3)

WORKING: tuple[RunState, ...] = ("planning", "testing", "building", "updating", "reviewing")
OPEN: tuple[RunState, ...] = (
    "pending",
    *WORKING,
    "awaiting_plan_approval",
    "awaiting_merge",
    "needs_you",
    "paused",
)

LEGAL: set[tuple[RunState, RunState]] = {
    # The happy path; the rebase onto main comes before review (design, git flow step 3).
    ("pending", "planning"),
    ("planning", "awaiting_plan_approval"),
    ("awaiting_plan_approval", "testing"),
    ("testing", "building"),
    ("building", "updating"),
    ("updating", "reviewing"),
    ("reviewing", "awaiting_merge"),
    ("awaiting_merge", "merged"),
    ("merged", "closed"),
    # Loops back from gates and decisions.
    ("awaiting_plan_approval", "planning"),  # plan sent back with notes
    ("updating", "building"),  # rebase conflict: the builder resolves it
    ("reviewing", "building"),  # Important finding: resume the builder
    # Feedback reruns from the earliest stage it affects.
    ("testing", "planning"),
    ("building", "planning"),
    ("building", "testing"),
    ("updating", "planning"),
    ("updating", "testing"),
    ("reviewing", "planning"),
    ("reviewing", "testing"),
    ("awaiting_merge", "planning"),
    ("awaiting_merge", "testing"),
    ("awaiting_merge", "building"),
    # Needs you: caps, stalls, questions, readiness, merge conflicts. Back into a working stage.
    *(("pending", "needs_you"),),
    *((state, "needs_you") for state in WORKING),
    ("awaiting_merge", "needs_you"),
    *(("needs_you", state) for state in WORKING),
    # Paused: usage limit, turn or time cap, while an agent works. Resumes the same stage.
    *((state, "paused") for state in WORKING),
    *(("paused", state) for state in WORKING),
    ("paused", "needs_you"),
    # Failed or stopped from any open state; then only closing is left.
    *((state, "failed") for state in OPEN),
    *((state, "stopped") for state in OPEN),
    ("failed", "closed"),
    ("stopped", "closed"),
}

ALL_STATES: tuple[RunState, ...] = get_args(RunState)
ALL_PAIRS = [(a, b) for a in ALL_STATES for b in ALL_STATES]


def _run(state: RunState = "pending") -> Run:
    run = new_run(
        run_id=3,
        slug="status-json",
        repo="app",
        kind="feature",
        provider="subscription",
        isolation="worktree",
        now=NOW,
    )
    return run.model_copy(update={"state": state})


def test_the_table_names_all_fourteen_states() -> None:
    assert len(ALL_STATES) == 14
    assert set(TRANSITIONS) == set(ALL_STATES)


@pytest.mark.parametrize(("source", "target"), ALL_PAIRS, ids=[f"{a}->{b}" for a, b in ALL_PAIRS])
def test_every_pair_is_legal_exactly_when_the_table_says(
    source: RunState, target: RunState
) -> None:  # AC
    run = _run(source)
    if (source, target) in LEGAL:
        moved, event = transition(run, target, now=LATER)
        assert moved.state == target
        assert moved.updated_at == LATER
        assert event == StateChanged(from_state=source, to_state=target)
    else:
        with pytest.raises(IllegalTransitionError) as exc:
            transition(run, target, now=LATER)
        assert source in str(exc.value)
        assert target in str(exc.value)


def test_the_module_table_matches_the_agreed_table() -> None:
    assert {(a, b) for a, targets in TRANSITIONS.items() for b in targets} == LEGAL


def test_merged_is_reached_only_from_awaiting_merge() -> None:
    """No path merges without your approval: only the approval wait leads to merged."""
    assert {a for a, b in LEGAL if b == "merged"} == {"awaiting_merge"}


def test_reviewing_is_the_only_way_into_awaiting_merge() -> None:
    assert {a for a, b in LEGAL if b == "awaiting_merge"} == {"reviewing"}


def test_closed_is_terminal() -> None:
    assert not [b for a, b in LEGAL if a == "closed"]


def test_staying_in_the_same_state_is_not_a_transition() -> None:
    with pytest.raises(IllegalTransitionError):
        transition(_run("building"), "building", now=LATER)


def test_transition_is_pure() -> None:
    run = _run("pending")
    moved, _ = transition(run, "planning", now=LATER)
    assert run.state == "pending"
    assert run.updated_at == NOW
    assert moved is not run


def test_transition_refuses_a_time_without_a_timezone() -> None:
    """PR #15 review: model_copy skips validation, so a naive `now` must be refused first."""
    with pytest.raises(ValidationError):
        transition(_run("pending"), "planning", now=datetime(2026, 10, 10, 9, 30))


def test_a_moved_run_saves_and_loads(tmp_path: Path) -> None:
    moved, _ = transition(_run("pending"), "planning", now=LATER)
    save_run(moved, tmp_path)
    assert load_run(tmp_path) == moved


def _log(folder: Path, *times: datetime) -> EventLog:
    ticks = iter(times)
    return EventLog(folder, repo="app", run_id=3, clock=lambda: next(ticks))


def test_move_emits_one_state_changed_event(tmp_path: Path) -> None:  # every transition emits
    run = _run("pending")
    moved = move(run, "planning", log=_log(tmp_path, LATER))
    events = read_events(tmp_path)
    assert moved.state == "planning"
    assert moved.updated_at == LATER  # one clock for the run and its event
    assert len(events) == 1
    event = events[0]
    assert event.body == StateChanged(from_state="pending", to_state="planning")
    assert (event.repo, event.run_id, event.role, event.at) == ("app", 3, None, LATER)


def test_move_writes_nothing_for_an_illegal_transition(tmp_path: Path) -> None:
    with pytest.raises(IllegalTransitionError):
        move(_run("closed"), "planning", log=_log(tmp_path, LATER))
    assert read_events(tmp_path) == []


def test_move_refuses_a_log_for_another_run(tmp_path: Path) -> None:
    other = EventLog(tmp_path, repo="app", run_id=4, clock=lambda: LATER)
    with pytest.raises(EventLogError, match="run"):
        move(_run("pending"), "planning", log=other)
    assert read_events(tmp_path) == []


def test_a_sequence_of_moves_logs_each_transition_in_order(tmp_path: Path) -> None:
    run = _run("pending")
    path: list[RunState] = ["planning", "awaiting_plan_approval", "testing", "building", "updating"]
    log = _log(tmp_path, *(NOW + timedelta(seconds=step) for step in range(1, 6)))
    for state in path:
        run = move(run, state, log=log)
    events = read_events(tmp_path)
    before: list[RunState] = ["pending", *path[:-1]]
    assert [e.seq for e in events] == [1, 2, 3, 4, 5]
    assert [e.body for e in events] == [
        StateChanged(from_state=a, to_state=b) for a, b in zip(before, path, strict=True)
    ]
