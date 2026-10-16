"""Your decisions on a waiting run (design: human decisions and feedback).

Every plan waits in `awaiting_plan_approval` (`Pipeline.advance` stops with `plan_approval`,
`questions` or `split`). From there:

- Plan approval: `approve_plan` (→ testing), `edit_and_approve` (your text replaces `plan.md`
  once it passes the planner gate; → testing) or `send_back` with notes (→ planning).
- Planner questions: `answer_questions` appends your answers to `intent.md` under a dated
  heading (→ planning).
- Split approval: splits arrive in milestone 3. Until then `settle` (or `hold_split`) moves a run
  whose plan proposes a split to needs_you.
- Merge approval arrives with task 2.22, as another action on `awaiting_merge`.

Each action loads the run from `run.json`, checks it waits for this decision and that what you
gave is usable, and refuses with `DecisionError` otherwise, leaving the run, its files and its
events as they were. Then it writes what it must, logs a `decision` event and the state change,
and saves `run.json` and the registry. Actions don't run agents: the caller builds a `Pipeline`
from `Decided.run` and calls `advance(note=Decided.note)`, then `settle` on the result, so the
planner's rerun gets your notes or answers.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from factory_engine.agents.runner import RunIndex, persist
from factory_engine.errors import DecisionError
from factory_engine.events import Decision, DecisionAction, EventLog, NeedsYou
from factory_engine.gates.plan import PLAN_FILE, PlanCheck, Question, check_plan, check_plan_text
from factory_engine.intake import ANSWERS_HEADING, INTENT_FILE, write_bytes_atomic
from factory_engine.pipeline import PipelineResult, PlanStop, plan_stop
from factory_engine.run import Run, RunState, load_run
from factory_engine.states import WORKING, move

SPLIT_REASON = (
    "The plan proposes a split into {parts} parts, and splits arrive in milestone 3. Send the "
    "planner feedback asking for one plan without a split, or stop the run."
)
_WAITING: RunState = "awaiting_plan_approval"
_INDENT = "    "  # an answer's later lines: inside its list item, never a heading


@dataclass(frozen=True)
class DecisionContext:
    """The run a decision acts on: its folder (`run.json`, `plan.md`, `intent.md`), its event
    log, the registry and a clock (for the dated answers heading)."""

    run_folder: Path
    events: EventLog
    registry: RunIndex
    clock: Callable[[], datetime]


@dataclass(frozen=True)
class Decided:
    """The run after a decision, as saved, and the note for the next agent: pass it to
    `Pipeline.advance(note=...)`. None when there is nothing to tell it."""

    run: Run
    note: str | None


@dataclass(frozen=True)
class Waiting:
    """What a run in `awaiting_plan_approval` waits for, and the planner gate's reading of
    `plan.md` now."""

    kind: PlanStop
    plan: PlanCheck


def waiting_for(ctx: DecisionContext) -> Waiting:
    """Which decision the run waits for. Raises DecisionError if it isn't waiting for one."""
    return _waiting(ctx, _load(ctx), "decided on")


# ------------------------------------------------------------------ plan approval


def approve_plan(ctx: DecisionContext) -> Decided:
    """Approve `plan.md` as it is: the run goes on to the tester."""
    run = _load(ctx)
    waiting = _waiting(ctx, run, "approved")
    if not waiting.plan.passed:
        raise DecisionError(
            f"{_plan_path(ctx)} no longer passes the planner gate, so it can't be approved: "
            f"{waiting.plan.reason} Edit and approve a fixed plan, or send it back with notes."
        )
    if waiting.kind == "split":
        raise DecisionError(_split_refusal(waiting.plan))
    if waiting.kind == "questions":
        count = len(waiting.plan.questions or ())
        raise DecisionError(
            f"The plan asks {count} questions, so it can't be approved yet. Answer them (an "
            "empty answer keeps the planner's default), edit and approve the plan, or send it "
            "back with notes."
        )
    return _decide(ctx, run, "approve_plan", None, "testing", note=None)


def edit_and_approve(ctx: DecisionContext, plan_text: str) -> Decided:
    """Replace `plan.md` with your edited text and approve it. The edit must pass the planner
    gate and may not ask questions or propose a split; otherwise it is refused with the reason
    and the run keeps waiting."""
    run = _load(ctx)
    _waiting(ctx, run, "edited and approved")
    check = check_plan_text(plan_text)
    if not check.passed:
        raise DecisionError(
            f"Your edited plan fails the planner gate, so it wasn't saved:\n{check.reason}\n"
            "Fix it and approve again; the run keeps waiting."
        )
    if check.split is not None:
        raise DecisionError(
            "Your edited plan still has a Split section, and splits arrive in milestone 3. "
            "Remove the section and approve again; the run keeps waiting."
        )
    if check.questions is not None:
        raise DecisionError(
            "Your edited plan still has a Questions section. Settle them in the plan and "
            "remove the section, or answer them instead; the run keeps waiting."
        )
    path = _plan_path(ctx)
    try:
        write_bytes_atomic(path, plan_text.encode("utf-8"))
    except OSError as err:
        raise DecisionError(
            f"{path}: couldn't write your edited plan ({err}). The run keeps waiting; check "
            "the run folder and approve again."
        ) from err
    ctx.events.append(check.event())
    return _decide(ctx, run, "edit_and_approve", None, "testing", note=None)


def send_back(ctx: DecisionContext, notes: str) -> Decided:
    """Send the plan back with your notes: the planner reruns and is told them."""
    run = _load(ctx)
    _waiting(ctx, run, "sent back")
    text = notes.strip()
    if not text:
        raise DecisionError(
            "Sending a plan back needs notes saying what to change. Write them and send it "
            "back again."
        )
    note = (
        "I sent your plan back. Read plan.md in the run folder again and revise it to address "
        f"these notes:\n\n{text}"
    )
    return _decide(ctx, run, "send_back", text, "planning", note=note)


# ------------------------------------------------------------------ planner questions


def answer_questions(ctx: DecisionContext, answers: Sequence[str]) -> Decided:
    """Answer the plan's questions, one answer per question in order; an empty answer keeps the
    planner's default. The answers are appended to `intent.md` under a dated heading and the
    planner reruns with them."""
    run = _load(ctx)
    waiting = _waiting(ctx, run, "answered")
    questions = waiting.plan.questions
    if not questions:
        raise DecisionError(
            "The plan asks no questions, so there is nothing to answer. Approve it, edit and "
            "approve it, or send it back with notes."
        )
    if len(answers) != len(questions):
        raise DecisionError(
            f"The plan asks {len(questions)} questions, and {len(answers)} answers were given. "
            "Give one answer per question, in order; an empty answer keeps the planner's "
            "default."
        )
    stamp = ctx.clock().astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    heading = f"### Answers, {stamp}"
    items = [_answer(n, q, a) for n, (q, a) in enumerate(zip(questions, answers, strict=True), 1)]
    block = "\n".join([heading, "", *items])
    path = ctx.run_folder / INTENT_FILE
    try:
        intent = path.read_bytes()
        write_bytes_atomic(path, _with_answers(intent, block))
    except OSError as err:
        raise DecisionError(
            f"{path}: couldn't add your answers ({err}). The run keeps waiting; check the run "
            "folder and answer again."
        ) from err
    note = (
        f"Your questions are answered in intent.md, under `{heading}`:\n\n{block}\n\n"
        "Write plan.md again with these answers. Leave out the Questions section unless "
        "something is still unclear."
    )
    return _decide(ctx, run, "answer_questions", block, "planning", note=note)


# ------------------------------------------------------------------ splits


def hold_split(ctx: DecisionContext) -> Decided:
    """A plan that proposes a split: the run needs you, since splits arrive in milestone 3."""
    run = _load(ctx)
    waiting = _waiting(ctx, run, "split")
    if not waiting.plan.split:
        raise DecisionError(
            "The plan proposes no split. Approve it, edit and approve it, answer its "
            "questions, or send it back with notes."
        )
    moved = move(run, "needs_you", log=ctx.events)
    ctx.events.append(NeedsYou(reason=SPLIT_REASON.format(parts=len(waiting.plan.split))))
    persist(moved, ctx.run_folder, ctx.registry)
    return Decided(run=moved, note=None)


def settle(ctx: DecisionContext, result: PipelineResult) -> PipelineResult:
    """What follows `Pipeline.advance` without you: a split needs you (milestone 2). Any other
    result is returned as it is."""
    if result.stop != "split":
        return result
    return PipelineResult(hold_split(ctx).run, "needs_you")


# ------------------------------------------------------------------ shared


def _load(ctx: DecisionContext) -> Run:
    run = load_run(ctx.run_folder)
    if (ctx.events.repo, ctx.events.run_id) != (run.repo, run.id):
        raise DecisionError(
            f"event log for run {ctx.events.repo}/{ctx.events.run_id} can't record run "
            f"{run.repo}/{run.id}. Pass the run's own event log."
        )
    return run


def _waiting(ctx: DecisionContext, run: Run, verb: str) -> Waiting:
    """The decision `run` waits for; refuses when it isn't waiting for plan approval."""
    if run.state != _WAITING:
        raise DecisionError(
            f"run {run.repo}/{run.id} is {run.state}, not waiting for plan approval, so its "
            f"plan can't be {verb}. {_hint(run.state)}"
        )
    plan = check_plan(_plan_path(ctx))
    return Waiting(plan_stop(plan), plan)


def _hint(state: RunState) -> str:
    if state == "pending" or state in WORKING:
        return "It is still working; decide when it stops for plan approval."
    if state == "paused":
        return "It is paused; resume it, then decide when it stops for plan approval."
    if state == "needs_you":
        return "It needs you: read the reason in its events, then resume or stop it."
    if state == "awaiting_merge":
        return "Its plan was approved already; it waits for your merge decision."
    return "It has ended; start a new run instead."


def _decide(
    ctx: DecisionContext,
    run: Run,
    action: DecisionAction,
    text: str | None,
    target: RunState,
    *,
    note: str | None,
) -> Decided:
    """Log the decision, move the run and save it."""
    ctx.events.append(Decision(action=action, text=text))
    moved = move(run, target, log=ctx.events)
    persist(moved, ctx.run_folder, ctx.registry)
    return Decided(run=moved, note=note)


def _plan_path(ctx: DecisionContext) -> Path:
    return ctx.run_folder / PLAN_FILE


def _split_refusal(plan: PlanCheck) -> str:
    parts = len(plan.split or ())
    return (
        f"The plan proposes a split into {parts} parts, so it can't be approved: splits arrive "
        "in milestone 3. Edit and approve it as one change, or send it back with notes."
    )


def _answer(number: int, question: Question, answer: str) -> str:
    """`- Q<n>: <question>` and `  A<n>: <answer>`, the answer's later lines indented so no
    line of yours can become a heading."""
    lines = answer.replace("\r\n", "\n").replace("\r", "\n").strip().split("\n")
    if lines == [""]:
        default = (question.default or "").rstrip(". ")
        lines = [
            f"No answer: go with your default ({default})."
            if default
            else "No answer: decide as you think best, and say so in the plan."
        ]
    first, *rest = lines
    later = [f"{_INDENT}{line}" if line.strip() else "" for line in rest]
    return "\n".join([f"- Q{number}: {question.text}", f"  A{number}: {first}", *later])


def _with_answers(intent: bytes, block: str) -> bytes:
    """`intent.md` with `block` appended after a blank line. The bytes before stay as they are;
    an `intent.md` with no `## Answers` line gets one first, so it stays the last heading."""
    out = intent if not intent or intent.endswith(b"\n") else intent + b"\n"
    if ANSWERS_HEADING not in (line.rstrip(b"\r") for line in out.split(b"\n")):
        out += b"\n" + ANSWERS_HEADING + b"\n"
    return out + b"\n" + block.encode("utf-8") + b"\n"
