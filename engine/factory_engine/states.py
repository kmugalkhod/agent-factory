"""The run state machine: which state changes are legal, as a pure function plus a logged move.

The happy path is pending → planning → awaiting_plan_approval → testing → building → updating
→ reviewing → awaiting_merge → merged → closed. The rebase onto main (`updating`) comes before
review, so the reviewer sees what will merge (design, git flow step 3). A rebase needed at merge
time, because main moved after approval, happens inside the merge action; a conflict there goes
to needs_you. So `merged` is reached only from `awaiting_merge`, and `awaiting_merge` only from
`reviewing`.

Staying in a state is not a transition: retries and resumed sessions keep the run's state.
"""

from datetime import datetime
from types import MappingProxyType

from factory_engine.errors import EventLogError, IllegalTransitionError
from factory_engine.events import EventLog, StateChanged
from factory_engine.run import Run, RunState

WORKING: tuple[RunState, ...] = ("planning", "testing", "building", "updating", "reviewing")
"""States in which an agent or the engine is working on the run."""

_STOP: tuple[RunState, ...] = ("failed", "stopped")

TRANSITIONS: MappingProxyType[RunState, frozenset[RunState]] = MappingProxyType(
    {
        "pending": frozenset({"planning", "needs_you", *_STOP}),
        "planning": frozenset({"awaiting_plan_approval", "needs_you", "paused", *_STOP}),
        # Approve, or send the plan back with notes.
        "awaiting_plan_approval": frozenset({"testing", "planning", *_STOP}),
        # Feedback reruns from the earliest stage it affects (design: talking to an agent).
        "testing": frozenset({"building", "planning", "needs_you", "paused", *_STOP}),
        "building": frozenset({"updating", "planning", "testing", "needs_you", "paused", *_STOP}),
        # A rebase conflict goes back to the builder.
        "updating": frozenset(
            {"reviewing", "building", "planning", "testing", "needs_you", "paused", *_STOP}
        ),
        # An Important finding resumes the builder.
        "reviewing": frozenset(
            {"awaiting_merge", "building", "planning", "testing", "needs_you", "paused", *_STOP}
        ),
        # Merge on your approval; a merge-time rebase conflict needs you.
        "awaiting_merge": frozenset(
            {"merged", "planning", "testing", "building", "needs_you", *_STOP}
        ),
        "needs_you": frozenset({*WORKING, *_STOP}),
        # Usage limit or a turn or time cap: resume the same stage, or ask you.
        "paused": frozenset({*WORKING, "needs_you", *_STOP}),
        "merged": frozenset({"closed"}),
        "failed": frozenset({"closed"}),
        "stopped": frozenset({"closed"}),
        "closed": frozenset(),
    }
)


def transition(run: Run, target: RunState, *, now: datetime) -> tuple[Run, StateChanged]:
    """The run moved to `target`, and the event that records it. Raises if not allowed."""
    allowed = TRANSITIONS[run.state]
    if target not in allowed:
        options = ", ".join(sorted(allowed)) or "none, the run is closed"
        raise IllegalTransitionError(
            f"run {run.repo}/{run.id}: can't go from {run.state} to {target}. "
            f"From {run.state} it can go to: {options}."
        )
    moved = run.model_copy(update={"state": target, "updated_at": now})
    return moved, StateChanged(from_state=run.state, to_state=target)


def move(run: Run, target: RunState, *, log: EventLog) -> Run:
    """Apply a transition and append its `state_changed` event to the run's log.

    The event's timestamp becomes the run's `updated_at`, so both use the log's clock.
    """
    if (log.repo, log.run_id) != (run.repo, run.id):
        raise EventLogError(
            f"event log for run {log.repo}/{log.run_id} can't record run {run.repo}/{run.id}. "
            "Pass the run's own event log."
        )
    transition(run, target, now=run.updated_at)  # check first, so nothing is logged if illegal
    event = log.append(StateChanged(from_state=run.state, to_state=target))
    moved, _ = transition(run, target, now=event.at)
    return moved
