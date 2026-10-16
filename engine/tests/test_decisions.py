"""Tests for factory_engine.decisions (task 2.21): plan approval, planner questions and splits.

Each test starts a real run through intake (a temporary repo with a bare "remote"), lets a
scripted fake planner write `plan.md` through the real pipeline (post-stage check, planner gate,
stage commit), and then decides. Only the planner runs, so no test command is needed.
"""

import asyncio
import os
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from factory_engine import events as ev
from factory_engine.agents.fake import FakeAgentRunner, result, system_init
from factory_engine.agents.runner import AgentResult, AgentSpec, SessionRecorder, not_running
from factory_engine.config import default_settings
from factory_engine.decisions import (
    SPLIT_REASON,
    Decided,
    DecisionContext,
    answer_questions,
    approve_plan,
    edit_and_approve,
    hold_split,
    send_back,
    settle,
    waiting_for,
)
from factory_engine.errors import DecisionError
from factory_engine.events import read_events
from factory_engine.gates.plan import check_plan_text
from factory_engine.git_ops import Git
from factory_engine.intake import INTENT_FILE, Intake, Started, request_of
from factory_engine.pipeline import Pipeline, PipelineResult, Places
from factory_engine.providers import get_profile
from factory_engine.registry import Registry, registry_path
from factory_engine.run import Run, RunState, load_run, save_run

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
REPO = "pilot"
FACTORY = Path(__file__).resolve().parents[2]
PARENT_ENV = {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": "test-oauth-token"}

PLAN = """# Plan: add mul

## Context

`src/calc.py` has `add`; the request asks for `mul`.

## Past runs used

None

## Files

- `src/calc.py`: add `mul`
- `tests/test_mul.py`: tests for `mul` (tester)

## Steps

1. Add `mul(a, b)` to `src/calc.py`.

## Acceptance criteria

1. `mul(2, 3)` returns 6.

## Test cases

- AC1: `test_mul`: `mul(2, 3)` is 6
"""
EDITED = PLAN.replace(
    "1. `mul(2, 3)` returns 6.\n", "1. `mul(2, 3)` returns 6.\n2. `mul(2, 0)` returns 0.\n"
)
BAD_PLAN = PLAN.replace(
    "- `src/calc.py`: add `mul`\n- `tests/test_mul.py`: tests for `mul` (tester)\n", "None\n"
)
QUESTIONS = (
    "\n## Questions\n\n1. Should `mul` take floats? Why it matters: the tests differ. "
    "Default if unanswered: yes.\n2. Should `mul` check types?\n"
)
SPLIT = "\n## Split\n\n1. Mul: add `mul`, depends on none\n2. Div: add `div`, depends on part 1\n"
SPEC = b"# Spec\r\n\r\nAdd mul. Caf\xe9 \xff\r\nNo newline at the end"


# ------------------------------------------------------------------ fakes


@dataclass(frozen=True)
class Act:
    """One planner run: the run docs it writes."""

    docs: dict[str, str] = field(default_factory=dict)


def planner(plan: str = PLAN) -> Act:
    return Act({"plan.md": plan, "handoff-planner.md": "# Handoff\n\n## Files touched\n\nNone\n"})


class Agents:
    """An `AgentRunner` that plays one planner `Act` per agent run, in order."""

    def __init__(self, acts: Sequence[Act]) -> None:
        self.acts = list(acts)
        self.specs: list[AgentSpec] = []
        self._fake: FakeAgentRunner | None = None

    async def run(self, spec: AgentSpec, recorder: SessionRecorder) -> AgentResult:
        assert self.acts, f"no act left in the script for the {spec.role}"
        assert spec.role == "planner", f"only the planner runs in these tests, not the {spec.role}"
        act = self.acts.pop(0)
        self.specs.append(spec)
        for name, text in act.docs.items():
            (spec.run_folder / name).write_bytes(text.encode("utf-8"))
        session = f"planner-{len(self.specs)}"
        self._fake = FakeAgentRunner([[system_init(session), result(session, 100, 20)]])
        return await self._fake.run(spec, recorder)

    async def interrupt(self) -> None:
        if self._fake is None:
            raise not_running()
        await self._fake.interrupt()

    async def send(self, text: str) -> None:
        if self._fake is None:
            raise not_running()
        await self._fake.send(text)


async def _no_sleep(seconds: float) -> None:
    await asyncio.sleep(0)


# ------------------------------------------------------------------ the world


def sh(cwd: Path, *args: str, env: dict[str, str]) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        timeout=60,
        encoding="utf-8",
        errors="replace",
    )
    return done.stdout.strip()


class World:
    """A repo cloned from a bare remote, the data folder, and one run started by intake."""

    def __init__(self, tmp_path: Path, git_env: dict[str, str], spec: bytes | None) -> None:
        self.env = git_env
        remote = tmp_path / "remote.git"
        sh(tmp_path, "init", "--bare", "-b", "main", str(remote), env=git_env)
        seed = tmp_path / "seed"
        sh(tmp_path, "init", "-b", "main", str(seed), env=git_env)
        sh(seed, "remote", "add", "origin", str(remote), env=git_env)
        (seed / "README.md").write_text("pilot\n", encoding="utf-8")
        sh(seed, "add", "-A", env=git_env)
        sh(seed, "commit", "-m", "first", env=git_env)
        sh(seed, "push", "origin", "main", env=git_env)
        self.repo = tmp_path / "repo"
        sh(tmp_path, "clone", str(remote), str(self.repo), env=git_env)
        self.data = tmp_path / "data"
        self.data.mkdir()
        self.skills = tmp_path / "skills"  # no factory skills to copy
        self.skills.mkdir()
        self.registry = Registry(registry_path(self.data))
        self.settings = default_settings(sys.platform)
        self.git = Git(git_env)
        intake = Intake(
            data=self.data,
            repo=REPO,
            repo_path=self.repo,
            settings=self.settings,
            git=self.git,
            registry=self.registry,
            clock=self.clock,
        )
        if spec is None:
            self.started: Started = intake.feature("Add mul to calc", requested_by="Kunal")
        else:
            path = tmp_path / "spec.md"
            path.write_bytes(spec)
            self.started = intake.spec(path, requested_by="Kunal")
        self.run_folder = self.started.run_folder
        self.events = self.started.events
        self.ctx = DecisionContext(
            run_folder=self.run_folder,
            events=self.events,
            registry=self.registry,
            clock=self.clock,
        )

    @staticmethod
    def clock() -> datetime:
        return NOW

    def advance(self, agents: Agents, note: str | None = None) -> PipelineResult:
        """Build a pipeline from the run as saved and carry it on, as the CLI does."""
        pipeline = Pipeline(
            load_run(self.run_folder),
            places=Places(
                repo=self.repo,
                data=self.data,
                worktree=self.started.worktree,
                run_folder=self.run_folder,
                prompts=FACTORY / "prompts",
                skills=self.skills,
            ),
            settings=self.settings,
            profile=get_profile("subscription"),
            parent_env=PARENT_ENV,
            git=self.git,
            runner=agents,
            events=self.events,
            registry=self.registry,
            clock=self.clock,
            sleep=_no_sleep,
        )
        return asyncio.run(pipeline.advance(note=note))

    def waiting(self, plan: str = PLAN) -> PipelineResult:
        """Plan the run: it waits for your decision on `plan`."""
        done = self.advance(Agents([planner(plan)]))
        assert done.run.state == "awaiting_plan_approval"
        return done

    def run(self) -> Run:
        return load_run(self.run_folder)

    def set_state(self, state: RunState) -> None:
        run = self.run().model_copy(update={"state": state})
        save_run(run, self.run_folder)
        self.registry.upsert_run(run)

    def intent(self) -> bytes:
        return (self.run_folder / INTENT_FILE).read_bytes()

    def plan(self) -> str:
        return (self.run_folder / "plan.md").read_bytes().decode("utf-8")

    def bodies(self, after: int = 0) -> list[ev.Body]:
        return [event.body for event in read_events(self.run_folder, after)]

    def last_seq(self) -> int:
        found = read_events(self.run_folder)
        return found[-1].seq if found else 0


type Maker = Callable[..., World]


@pytest.fixture
def world(tmp_path: Path, git_env: dict[str, str]) -> Iterator[Maker]:
    made: list[World] = []

    def make(spec: bytes | None = None) -> World:
        made.append(World(tmp_path, git_env, spec))
        return made[-1]

    yield make
    for each in made:
        each.registry.close()


def moved(old: RunState, new: RunState) -> ev.StateChanged:
    return ev.StateChanged(from_state=old, to_state=new)


# ------------------------------------------------------------------ plan approval


def test_approve_moves_the_run_on_to_testing(world: Maker) -> None:
    w = world()
    w.waiting()
    seq = w.last_seq()

    decided = approve_plan(w.ctx)

    assert decided == Decided(run=w.run(), note=None)
    assert decided.run.state == "testing"
    assert w.registry.get_run(REPO, decided.run.id) == decided.run
    assert w.bodies(seq) == [
        ev.Decision(action="approve_plan"),
        moved("awaiting_plan_approval", "testing"),
    ]
    assert w.plan() == PLAN


def test_edit_and_approve_replaces_the_plan_and_moves_to_testing(world: Maker) -> None:
    w = world()
    w.waiting()
    seq = w.last_seq()

    decided = edit_and_approve(w.ctx, EDITED)

    assert decided.run.state == "testing"
    assert w.plan() == EDITED
    assert w.bodies(seq) == [
        check_plan_text(EDITED).event(),
        ev.Decision(action="edit_and_approve"),
        moved("awaiting_plan_approval", "testing"),
    ]


@pytest.mark.parametrize(
    ("edited", "reason"),
    [
        (BAD_PLAN, check_plan_text(BAD_PLAN).reason),
        (PLAN + QUESTIONS, "Questions"),
        (PLAN + SPLIT, "Split"),
    ],
    ids=["fails-the-gate", "asks-questions", "proposes-a-split"],
)
def test_an_edit_the_run_cant_go_on_with_is_refused_and_the_run_keeps_waiting(
    world: Maker, edited: str, reason: str
) -> None:
    w = world()
    w.waiting()
    seq, before = w.last_seq(), w.run()

    with pytest.raises(DecisionError) as raised:
        edit_and_approve(w.ctx, edited)

    assert reason in str(raised.value)
    assert w.run() == before
    assert w.plan() == PLAN
    assert w.bodies(seq) == []


def test_send_back_passes_the_notes_to_the_planner(world: Maker) -> None:
    w = world()
    w.waiting()
    seq = w.last_seq()
    notes = "Name the function `multiply`, not `mul`."

    decided = send_back(w.ctx, notes)

    assert decided.run.state == "planning"
    assert decided.note is not None and notes in decided.note
    assert w.bodies(seq) == [
        ev.Decision(action="send_back", text=notes),
        moved("awaiting_plan_approval", "planning"),
    ]

    agents = Agents([planner(EDITED)])
    again = w.advance(agents, note=decided.note)

    [rerun] = agents.specs
    assert notes in rerun.prompt
    assert rerun.resume is None
    assert (again.stop, again.run.state) == ("plan_approval", "awaiting_plan_approval")
    assert again.run.attempts == {"planner": 2}


@pytest.mark.parametrize("notes", ["", "  \n "])
def test_send_back_needs_notes(world: Maker, notes: str) -> None:
    w = world()
    w.waiting()
    before = w.run()
    with pytest.raises(DecisionError, match="notes"):
        send_back(w.ctx, notes)
    assert w.run() == before


@pytest.mark.parametrize(("extra", "kind"), [(QUESTIONS, "questions"), (SPLIT, "split")])
def test_approve_is_refused_while_the_plan_asks_or_splits(
    world: Maker, extra: str, kind: str
) -> None:
    w = world()
    w.waiting(PLAN + extra)
    seq, before = w.last_seq(), w.run()

    with pytest.raises(DecisionError, match=kind):
        approve_plan(w.ctx)

    assert w.run() == before
    assert w.bodies(seq) == []


def test_approve_is_refused_when_plan_md_no_longer_passes(world: Maker) -> None:
    w = world()
    w.waiting()
    (w.run_folder / "plan.md").write_bytes(BAD_PLAN.encode("utf-8"))
    before = w.run()

    with pytest.raises(DecisionError, match="planner gate"):
        approve_plan(w.ctx)
    assert w.run() == before


# ------------------------------------------------------------------ planner questions


def test_answers_are_appended_under_a_dated_heading_and_the_planner_reruns(
    world: Maker,
) -> None:
    w = world()
    waiting = w.waiting(PLAN + QUESTIONS)
    assert waiting.stop == "questions"
    assert waiting_for(w.ctx).kind == "questions"
    intent_before, seq = w.intent(), w.last_seq()

    decided = answer_questions(w.ctx, ["No, integers only.", "Yes:\nraise TypeError."])

    block = (
        "\n### Answers, 2026-10-17 12:00 UTC\n"
        "\n"
        "- Q1: Should `mul` take floats?\n"
        "  A1: No, integers only.\n"
        "- Q2: Should `mul` check types?\n"
        "  A2: Yes:\n"
        "    raise TypeError.\n"
    )
    assert w.intent() == intent_before + block.encode("utf-8")
    assert decided.run.state == "planning"
    assert w.bodies(seq) == [
        ev.Decision(action="answer_questions", text=block.strip("\n")),
        moved("awaiting_plan_approval", "planning"),
    ]

    agents = Agents([planner(PLAN)])
    again = w.advance(agents, note=decided.note)

    [rerun] = agents.specs
    assert "intent.md" in rerun.prompt and "No, integers only." in rerun.prompt
    assert (again.stop, again.run.state) == ("plan_approval", "awaiting_plan_approval")


def test_a_blank_answer_keeps_the_planners_default(world: Maker) -> None:
    w = world()
    w.waiting(PLAN + QUESTIONS)

    answer_questions(w.ctx, ["", "No."])

    text = w.intent().decode("utf-8")
    assert "  A1: No answer: go with your default (yes).\n" in text
    assert "  A2: No.\n" in text


def test_a_second_round_of_answers_gets_its_own_heading(world: Maker) -> None:
    w = world()
    w.waiting(PLAN + QUESTIONS)
    decided = answer_questions(w.ctx, ["No.", "No."])
    w.advance(Agents([planner(PLAN + QUESTIONS)]), note=decided.note)

    answer_questions(w.ctx, ["Still no.", "Still no."])

    text = w.intent().decode("utf-8")
    assert text.count("### Answers, 2026-10-17 12:00 UTC\n") == 2
    assert text.index("A1: No.") < text.index("A1: Still no.")


def test_an_answer_cant_add_a_heading_to_intent_md(world: Maker) -> None:
    w = world()
    w.waiting(PLAN + QUESTIONS)

    answer_questions(w.ctx, ["Yes.\r\n## Answers\r\n## Request", "No."])

    intent = w.intent()
    assert b"\n    ## Answers\n    ## Request\n" in intent
    headings = [line for line in intent.split(b"\n") if line.startswith(b"## ")]
    assert headings == [b"## Request", b"## Answers"]


def test_answers_keep_a_specs_bytes_exact(world: Maker) -> None:
    w = world(SPEC)
    w.waiting(PLAN + QUESTIONS)

    answer_questions(w.ctx, ["Yes.", "No."])

    intent = w.intent()
    assert SPEC in intent
    assert request_of(intent) == SPEC
    assert intent.endswith(b"  A2: No.\n")


@pytest.mark.parametrize("answers", [[], ["Only one."], ["One.", "Two.", "Three."]])
def test_answers_must_match_the_questions(world: Maker, answers: list[str]) -> None:
    w = world()
    w.waiting(PLAN + QUESTIONS)
    intent, before, seq = w.intent(), w.run(), w.last_seq()

    with pytest.raises(DecisionError, match="2 questions"):
        answer_questions(w.ctx, answers)

    assert (w.intent(), w.run(), w.bodies(seq)) == (intent, before, [])


def test_answers_are_refused_when_the_plan_asks_nothing(world: Maker) -> None:
    w = world()
    w.waiting()
    intent = w.intent()
    with pytest.raises(DecisionError, match="no questions"):
        answer_questions(w.ctx, ["Yes."])
    assert w.intent() == intent


# ------------------------------------------------------------------ splits


def test_a_split_marks_the_run_needs_you(world: Maker) -> None:
    w = world()
    waiting = w.waiting(PLAN + SPLIT)
    assert waiting.stop == "split"
    seq = w.last_seq()

    settled = settle(w.ctx, waiting)

    assert (settled.stop, settled.run.state) == ("needs_you", "needs_you")
    assert settled.run == w.run() == w.registry.get_run(REPO, settled.run.id)
    assert w.bodies(seq) == [
        moved("awaiting_plan_approval", "needs_you"),
        ev.NeedsYou(reason=SPLIT_REASON.format(parts=2)),
    ]
    assert "splits arrive in milestone 3" in SPLIT_REASON


def test_settle_leaves_other_stops_alone(world: Maker) -> None:
    w = world()
    waiting = w.waiting()
    seq = w.last_seq()
    assert settle(w.ctx, waiting) == waiting
    assert w.bodies(seq) == []


def test_hold_split_is_refused_without_a_split(world: Maker) -> None:
    w = world()
    w.waiting()
    with pytest.raises(DecisionError, match="split"):
        hold_split(w.ctx)
    assert w.run().state == "awaiting_plan_approval"


# ------------------------------------------------------------------ wrong states

ACTIONS: dict[str, Callable[[DecisionContext], Decided]] = {
    "approve": approve_plan,
    "edit": lambda ctx: edit_and_approve(ctx, EDITED),
    "send_back": lambda ctx: send_back(ctx, "More tests."),
    "answer": lambda ctx: answer_questions(ctx, ["Yes.", "No."]),
    "split": hold_split,
}


@pytest.mark.parametrize(
    "state", ["pending", "planning", "testing", "needs_you", "paused", "awaiting_merge", "closed"]
)
def test_a_decision_on_a_run_that_isnt_waiting_is_refused(world: Maker, state: RunState) -> None:
    w = world()
    (w.run_folder / "plan.md").write_bytes((PLAN + SPLIT + QUESTIONS).encode("utf-8"))
    w.set_state(state)
    intent, plan, before, seq = w.intent(), w.plan(), w.run(), w.last_seq()

    for name, action in ACTIONS.items():
        with pytest.raises(DecisionError) as raised:
            action(w.ctx)
        message = str(raised.value)
        assert f"is {state}, not waiting for plan approval" in message, name
        assert (w.intent(), w.plan(), w.run(), w.bodies(seq)) == (intent, plan, before, [])
