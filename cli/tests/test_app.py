"""Tests for the `factory` command (task 2.28): each command calls the matching engine action on
a stub, exit codes (0 done, 2 needs you, 1 error) and the status listing."""

import json
import re
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from factory_cli.actions import Ending, Outcome, RunDetail, RunRow
from factory_cli.app import exit_code, make_app, parse_answers, run_command
from factory_engine import events as ev
from factory_engine.errors import DecisionError, FactoryError
from factory_engine.events import Body, Event, EventObserver
from factory_engine.run import Pause, Role, Run, RunState, new_run
from factory_engine.usage import Grouping, Usage, UsageGroup, UsageTotals

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
FOLDER = Path("data") / "runs" / "pilot" / "3-add-mul"
WORKTREE = Path("data") / "worktrees" / "pilot" / "3-add-mul"


def make_run(state: RunState = "awaiting_plan_approval", **update: Any) -> Run:
    run = new_run(
        run_id=3,
        slug="add-mul",
        repo="pilot",
        kind="feature",
        provider="subscription",
        isolation="worktree",
        now=NOW,
    )
    return run.model_copy(update={"state": state, **update})


def outcome(
    stop: Ending = "plan_approval",
    state: RunState = "awaiting_plan_approval",
    *,
    questions: tuple[str, ...] = (),
    reason: str | None = None,
) -> Outcome:
    return Outcome(make_run(state), stop, FOLDER, questions=questions, reason=reason)


class Stub:
    """An engine that records every action it is asked for and returns what it was given."""

    def __init__(
        self,
        result: Outcome | None = None,
        *,
        rows: Sequence[RunRow] = (),
        error: FactoryError | None = None,
        events: Sequence[tuple[Body, Role | None]] = (),
        usage: Usage | None = None,
    ) -> None:
        self.result = result or outcome()
        self.usage_result = usage or usage_of()
        self.rows = list(rows)
        self.error = error
        self.events = list(events)
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.repos: list[Path | None] = []
        self.observers: list[EventObserver | None] = []

    def factory(self, repo: Path | None, progress: EventObserver | None) -> "Stub":
        self.repos.append(repo)
        self.observers.append(progress)
        return self

    def _call(self, name: str, *args: object) -> None:
        self.calls.append((name, args))
        if self.error is not None:
            raise self.error
        observer = self.observers[-1] if self.observers else None
        if observer is not None:  # the run "works": its events reach the observer
            for seq, (body, role) in enumerate(self.events, 1):
                observer(Event(seq=seq, at=NOW, repo="pilot", run_id=3, role=role, body=body))

    def start_feature(self, description: str, *, slug: str | None) -> Outcome:
        self._call("start_feature", description, slug)
        return self.result

    def start_spec(self, path: Path, *, slug: str | None) -> Outcome:
        self._call("start_spec", path, slug)
        return self.result

    def runs(self, *, all_repos: bool) -> list[RunRow]:
        self._call("runs", all_repos)
        return self.rows

    def describe(self, run_id: int) -> RunDetail:
        self._call("describe", run_id)
        run = self.result.run
        return RunDetail(
            run=run,
            running=False,
            run_folder=FOLDER,
            worktree=WORKTREE,
            questions=self.result.questions,
            reason=self.result.reason,
        )

    def approve(self, run_id: int) -> Outcome:
        self._call("approve", run_id)
        return self.result

    def edit_and_approve(self, run_id: int, plan_file: Path) -> Outcome:
        self._call("edit_and_approve", run_id, plan_file)
        return self.result

    def send_back(self, run_id: int, notes: str) -> Outcome:
        self._call("send_back", run_id, notes)
        return self.result

    def answer(self, run_id: int, answers: Sequence[str]) -> Outcome:
        self._call("answer", run_id, list(answers))
        return self.result

    def stop(self, run_id: int) -> Run:
        self._call("stop", run_id)
        return make_run("stopped")

    def resume(self, run_id: int, *, note: str | None) -> Outcome:
        self._call("resume", run_id, note)
        return self.result

    def attach(self, run_id: int, *, role: Role | None) -> str:
        self._call("attach", run_id, role)
        return "claude --resume session-1"

    def usage(self, *, by: Grouping, since: date | None, all_repos: bool) -> Usage:
        self._call("usage", by, since, all_repos)
        return self.usage_result


def counts(
    tokens: tuple[int, int, int, int], *, turns: int = 1, runs: int = 1, hits: int = 0
) -> dict[str, Any]:  # Any: unpacked into UsageGroup next to its str fields
    i, o, r, c = tokens
    return {
        "turns": turns,
        "input": i,
        "output": o,
        "cache_read": r,
        "cache_creation": c,
        "total": i + o + r + c,
        "runs": runs,
        "usage_limit_hits": hits,
        "runs_hit_usage_limit": min(hits, runs),
    }


def usage_of(
    groups: Sequence[UsageGroup] = (), *, by: Grouping = "run", repo: str | None = "pilot"
) -> Usage:
    """A `factory usage` answer whose total sums `groups` (runs counted once per group)."""
    total = {name: sum(getattr(g, name) for g in groups) for name in UsageTotals.model_fields}
    return Usage(by=by, repo=repo, since=None, groups=list(groups), total=UsageTotals(**total))


def run_group(run_id: int, slug: str, tokens: tuple[int, int, int, int], **kw: int) -> UsageGroup:
    return UsageGroup(
        key=f"pilot/{run_id}", repo="pilot", run_id=run_id, slug=slug, **counts(tokens, **kw)
    )


def invoke(stub: Stub, *args: str) -> Any:
    return CliRunner().invoke(make_app(stub.factory), list(args))


# ------------------------------------------------------------------ start


def test_start_feature_calls_the_engine_and_says_what_the_plan_waits_for() -> None:
    stub = Stub()

    done = invoke(stub, "start", "--feature", "Add mul to calc")

    assert stub.calls == [("start_feature", ("Add mul to calc", None))]
    assert stub.repos == [None]
    assert done.exit_code == 2
    assert "factory approve 3" in done.output
    assert "--send-back" in done.output
    assert str(FOLDER / "plan.md") in done.output


def test_start_spec_passes_the_spec_file_and_slug(tmp_path: Path) -> None:
    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n", encoding="utf-8")
    stub = Stub()

    invoke(stub, "start", "--spec", str(spec), "--slug", "csv-export")

    assert stub.calls == [("start_spec", (spec, "csv-export"))]


def test_the_repo_option_reaches_the_engine(tmp_path: Path) -> None:
    stub = Stub()

    invoke(stub, "start", "--feature", "x", "--repo", str(tmp_path))

    assert stub.repos == [tmp_path]


@pytest.mark.parametrize(
    "args",
    [
        ["start"],
        ["start", "--feature", "x", "--spec", "spec.md"],
        ["start", "--feature", "   "],
    ],
)
def test_start_needs_exactly_one_request(args: list[str]) -> None:
    stub = Stub()

    done = invoke(stub, *args)

    assert done.exit_code == 1
    assert stub.calls == []
    assert "--feature" in done.output


def test_new_project_arrives_with_a_later_task() -> None:
    stub = Stub()

    done = invoke(stub, "start", "--new-project")

    assert done.exit_code == 1
    assert "2.26" in done.output
    assert stub.calls == []


def test_feedback_arrives_with_a_later_task() -> None:
    stub = Stub()

    done = invoke(stub, "feedback", "3", "use floats")

    assert done.exit_code == 1
    assert "2.23" in done.output
    assert stub.calls == []


# ------------------------------------------------------------------ exit codes


@pytest.mark.parametrize(
    ("stop", "state", "code"),
    [
        ("plan_approval", "awaiting_plan_approval", 2),
        ("questions", "awaiting_plan_approval", 2),
        ("split", "awaiting_plan_approval", 2),
        ("needs_you", "needs_you", 2),
        ("paused", "paused", 2),
        ("awaiting_merge", "awaiting_merge", 2),
        ("interrupted", "building", 2),
        ("ended", "merged", 0),
        ("ended", "closed", 0),
        ("ended", "failed", 1),
        ("ended", "stopped", 1),
    ],
)
def test_exit_codes_follow_where_the_run_stopped(stop: Ending, state: RunState, code: int) -> None:
    assert exit_code(outcome(stop, state)) == code
    assert invoke(Stub(outcome(stop, state)), "resume", "3").exit_code == code


def test_an_engine_error_exits_1_with_its_message_and_no_traceback() -> None:
    stub = Stub(error=DecisionError("run pilot/3 is building, not waiting for plan approval."))

    done = invoke(stub, "approve", "3")

    assert done.exit_code == 1
    assert "not waiting for plan approval" in done.output
    assert "Traceback" not in done.output


def test_a_usage_error_exits_1_not_2() -> None:
    stub = Stub()

    assert run_command(make_app(stub.factory), ["start", "--no-such-option"]) == 1
    assert run_command(make_app(stub.factory), ["approve"]) == 1  # the run ID is missing
    assert stub.calls == []


def test_run_command_returns_the_commands_exit_code() -> None:
    stub = Stub(outcome("ended", "merged"))

    assert run_command(make_app(stub.factory), ["resume", "3"]) == 0
    assert run_command(make_app(Stub().factory), ["resume", "3"]) == 2


# ------------------------------------------------------------------ approve


def test_approve_approves_the_plan() -> None:
    stub = Stub(outcome("needs_you", "needs_you", reason="the builder used all 3 attempts"))

    done = invoke(stub, "approve", "3")

    assert stub.calls == [("approve", (3,))]
    assert done.exit_code == 2
    assert "the builder used all 3 attempts" in done.output


def test_approve_with_edit_passes_the_edited_plan_file(tmp_path: Path) -> None:
    plan = tmp_path / "plan.md"
    stub = Stub()

    invoke(stub, "approve", "3", "--edit", str(plan))

    assert stub.calls == [("edit_and_approve", (3, plan))]


def test_approve_with_send_back_passes_the_notes() -> None:
    stub = Stub()

    invoke(stub, "approve", "3", "--send-back", "Cover zero too.")

    assert stub.calls == [("send_back", (3, "Cover zero too."))]


def test_approve_with_answers_passes_them_in_order() -> None:
    stub = Stub()

    invoke(stub, "approve", "3", "--answer", "Yes, floats.", "--answer", "")

    assert stub.calls == [("answer", (3, ["Yes, floats.", ""]))]


def test_approve_with_an_answers_file_splits_it_on_dashes(tmp_path: Path) -> None:
    answers = tmp_path / "answers.md"
    answers.write_text("Yes, floats.\nAnd ints.\n---\n\n---\nNo.\n", encoding="utf-8")
    stub = Stub()

    invoke(stub, "approve", "3", "--answers", str(answers))

    assert stub.calls == [("answer", (3, ["Yes, floats.\nAnd ints.", "", "No."]))]


def test_an_unreadable_answers_file_is_an_error(tmp_path: Path) -> None:
    stub = Stub()

    done = invoke(stub, "approve", "3", "--answers", str(tmp_path / "missing.md"))

    assert done.exit_code == 1
    assert stub.calls == []


@pytest.mark.parametrize(
    "args",
    [
        ["--edit", "plan.md", "--send-back", "notes"],
        ["--send-back", "notes", "--answer", "yes"],
        ["--answer", "yes", "--answers", "answers.md"],
    ],
)
def test_approve_takes_one_kind_of_decision(args: list[str]) -> None:
    stub = Stub()

    done = invoke(stub, "approve", "3", *args)

    assert done.exit_code == 1
    assert stub.calls == []


def test_questions_are_listed_with_how_to_answer_them() -> None:
    asked = ("Should mul take floats? (default: yes)", "Should mul check types?")
    stub = Stub(outcome("questions", questions=asked))

    done = invoke(stub, "start", "--feature", "Add mul")

    assert done.exit_code == 2
    assert "Q1: Should mul take floats? (default: yes)" in done.output
    assert "Q2: Should mul check types?" in done.output
    assert "factory approve 3 --answer" in done.output


def test_parse_answers_keeps_blank_answers() -> None:
    assert parse_answers("a\r\n---\r\n\r\n---\nb\n") == ["a", "", "b"]
    assert parse_answers("only one\n") == ["only one"]


# ------------------------------------------------------------------ stop, resume, attach


def test_stop_stops_the_run() -> None:
    stub = Stub()

    done = invoke(stub, "stop", "3", "--repo", "elsewhere")

    assert stub.calls == [("stop", (3,))]
    assert stub.repos == [Path("elsewhere")]
    assert done.exit_code == 0
    assert "stopped" in done.output


def test_resume_carries_the_run_on_with_an_optional_note() -> None:
    stub = Stub()

    invoke(stub, "resume", "3")
    invoke(stub, "resume", "3", "--note", "The flaky test is fixed.")

    assert stub.calls == [("resume", (3, None)), ("resume", (3, "The flaky test is fixed."))]


def test_an_interrupted_run_says_how_to_carry_on() -> None:
    done = invoke(Stub(outcome("interrupted", "building")), "resume", "3")

    assert "factory resume 3" in done.output
    assert "factory stop 3" in done.output


def test_attach_prints_the_command() -> None:
    stub = Stub()

    done = invoke(stub, "attach", "3", "--role", "builder")

    assert stub.calls == [("attach", (3, "builder"))]
    assert done.exit_code == 0
    assert "claude --resume session-1" in done.output


def test_attach_refuses_an_unknown_role() -> None:
    stub = Stub()

    done = invoke(stub, "attach", "3", "--role", "boss")

    assert done.exit_code != 0
    assert stub.calls == []


# ------------------------------------------------------------------ status


def test_status_lists_runs_with_state_stage_attempts_and_tokens() -> None:
    building = make_run(
        "building",
        stage="builder",
        attempts={"planner": 1, "tester": 1, "builder": 2},
        metrics=make_run().metrics.model_copy(update={"tokens": {"planner": 1200, "builder": 34}}),
    )
    rows = [
        RunRow(building, running=True),
        RunRow(make_run("planning").model_copy(update={"id": 4, "slug": "fix"}), running=False),
    ]
    stub = Stub(rows=rows)

    done = invoke(stub, "status")

    assert stub.calls == [("runs", (False,))]
    assert done.exit_code == 0
    lines = done.output.splitlines()
    assert lines[0].split() == ["RUN", "STATE", "STAGE", "ATTEMPTS", "TOKENS"]
    assert "3-add-mul" in lines[1]
    assert "building" in lines[1]
    assert "builder" in lines[1]
    assert "planner 1, tester 1, builder 2" in lines[1]
    assert "1,234" in lines[1]
    assert "4-fix" in lines[2]
    assert "interrupted" in lines[2]  # in a working state, but no factory process has it


def test_status_all_lists_every_repo() -> None:
    stub = Stub(rows=[RunRow(make_run(), running=False)])

    done = invoke(stub, "status", "--all")

    assert stub.calls == [("runs", (True,))]
    assert "pilot/3-add-mul" in done.output


def test_status_with_no_runs_says_so() -> None:
    done = invoke(Stub(), "status")

    assert done.exit_code == 0
    assert "No runs" in done.output


def test_status_of_one_run_shows_its_details() -> None:
    paused = make_run(
        "paused",
        stage="builder",
        pause=Pause(
            reason="time_cap",
            detail="the builder ran for 120 minutes",
            state="building",
            role="builder",
            attempt=1,
            session_id="s-1",
            resume_at=None,
        ),
    )
    stub = Stub(Outcome(paused, "paused", FOLDER, reason="the builder ran for 120 minutes"))

    done = invoke(stub, "status", "3")

    assert stub.calls == [("describe", (3,))]
    assert done.exit_code == 0
    assert "paused" in done.output
    assert str(WORKTREE) in done.output
    assert "the builder ran for 120 minutes" in done.output


# ------------------------------------------------------------------ progress output

WORK: list[tuple[Body, Role | None]] = [
    (ev.StageStarted(stage="planner", attempt=1), "planner"),
    (ev.ToolCall(tool="Write", target="plan.md", decision="allowed", reason=None), "planner"),
    (ev.Message(sender="agent", text="Plan written."), "planner"),
    (ev.StageFinished(stage="planner", attempt=1, outcome="completed"), "planner"),
    (ev.GateResult(gate="plan", passed=True, reason="ok"), "planner"),
]
_TIME = r"\d\d:\d\d:\d\d"


@pytest.mark.parametrize(
    "args",
    [
        ["start", "--feature", "Add mul"],
        ["approve", "3"],
        ["approve", "3", "--send-back", "notes"],
        ["resume", "3"],
    ],
)
def test_run_driving_commands_print_progress_before_the_summary(args: list[str]) -> None:
    stub = Stub(events=WORK)

    done = invoke(stub, *args)

    lines = done.output.splitlines()
    assert re.fullmatch(_TIME + r" \[planner\] started attempt 1", lines[0])
    assert re.fullmatch(_TIME + r" \[planner\] attempt 1 completed", lines[1])
    assert re.fullmatch(_TIME + r" \[planner\] gate plan passed", lines[2])
    assert lines[3] == "run pilot/3 (add-mul): awaiting_plan_approval"
    assert "Write plan.md" not in done.output
    assert "Plan written." not in done.output


@pytest.mark.parametrize("flag", ["--verbose", "-v"])
def test_verbose_adds_tool_calls_and_agent_messages(flag: str) -> None:
    stub = Stub(events=WORK)

    done = invoke(stub, "start", "--feature", "Add mul", flag)

    assert re.search(_TIME + r" \[planner\] Write plan.md", done.output)
    assert re.search(_TIME + r" \[planner\] agent: Plan written.", done.output)


@pytest.mark.parametrize(
    "args", [["status"], ["status", "3"], ["stop", "3"], ["attach", "3"], ["usage"]]
)
def test_other_commands_watch_no_events(args: list[str]) -> None:
    stub = Stub(events=WORK)

    invoke(stub, *args)

    assert stub.observers == [None]


# ------------------------------------------------------------------ usage (2.29)

TWO_RUNS = usage_of(
    [
        run_group(3, "add-mul", (1_204, 8_331, 540_112, 25_010), turns=12, hits=1),
        run_group(4, "fix", (90, 400, 10_000, 2_000), turns=2),
    ]
)


def test_usage_lists_tokens_per_run_with_each_kind_apart() -> None:
    stub = Stub(usage=TWO_RUNS)

    done = invoke(stub, "usage")

    assert stub.calls == [("usage", ("run", None, False))]
    assert done.exit_code == 0
    lines = done.output.splitlines()
    assert "pilot" in lines[0]
    assert "by run" in lines[0]
    assert lines[1].split() == [
        "RUN", "TURNS", "INPUT", "OUTPUT", "CACHE", "READ", "CACHE", "WRITE", "TOTAL", "LIMIT",
        "HITS",
    ]  # fmt: skip
    assert lines[2].split() == [
        "3-add-mul", "12", "1,204", "8,331", "540,112", "25,010", "574,657", "1"
    ]  # fmt: skip
    assert lines[3].split() == ["4-fix", "2", "90", "400", "10,000", "2,000", "12,490", "0"]
    assert lines[4].split() == [
        "TOTAL", "14", "1,294", "8,731", "550,112", "27,010", "587,147", "1"
    ]  # fmt: skip


def test_usage_says_where_the_tokens_go_and_how_often_the_limit_was_hit() -> None:
    done = invoke(Stub(usage=TWO_RUNS), "usage")

    assert "cache read 93.7%" in done.output
    assert "input 0.2%" in done.output
    assert "Usage limit hit 1 time, in 1 of 2 runs." in done.output


def test_usage_passes_the_grouping_the_day_and_every_repo() -> None:
    roles = usage_of(
        [UsageGroup(key="planner", **counts((5, 5, 5, 5), runs=2))], by="role", repo=None
    )
    stub = Stub(usage=roles)

    done = invoke(stub, "usage", "--by", "role", "--since", "2026-10-14", "--all")

    assert stub.calls == [("usage", ("role", date(2026, 10, 14), True))]
    assert done.exit_code == 0
    assert "every repo" in done.output.splitlines()[0]
    assert done.output.splitlines()[1].split()[:3] == ["ROLE", "RUNS", "TURNS"]
    assert "planner" in done.output


def test_usage_for_every_repo_names_each_runs_repo() -> None:
    done = invoke(Stub(usage=usage_of(TWO_RUNS.groups, repo=None)), "usage", "--all")

    assert "pilot/3-add-mul" in done.output


@pytest.mark.parametrize("by", ["run", "repo", "role", "day"])
def test_usage_json_is_the_engines_answer(by: str) -> None:
    stub = Stub(usage=TWO_RUNS)

    done = invoke(stub, "usage", "--by", by, "--json")

    assert done.exit_code == 0
    assert stub.calls == [("usage", (by, None, False))]
    assert Usage.model_validate(json.loads(done.output)) == TWO_RUNS
    assert json.loads(done.output)["groups"][0]["cache_read"] == 540_112


def test_usage_with_nothing_recorded_says_so() -> None:
    done = invoke(Stub(), "usage")

    assert done.exit_code == 0
    assert "No token use recorded" in done.output


def test_usage_json_with_nothing_recorded_is_still_json() -> None:
    done = invoke(Stub(), "usage", "--json")

    assert json.loads(done.output)["groups"] == []


@pytest.mark.parametrize("since", ["yesterday", "2026-13-01", "14/10/2026"])
def test_usage_refuses_a_since_that_isnt_a_date(since: str) -> None:
    stub = Stub()

    done = invoke(stub, "usage", "--since", since)

    assert done.exit_code == 1
    assert "YYYY-MM-DD" in done.output
    assert stub.calls == []


def test_usage_refuses_an_unknown_grouping() -> None:
    stub = Stub()

    assert run_command(make_app(stub.factory), ["usage", "--by", "week"]) == 1
    assert stub.calls == []


def test_usage_engine_error_exits_1() -> None:
    done = invoke(Stub(error=FactoryError("registry.db: can't read the registry.")), "usage")

    assert done.exit_code == 1
    assert "can't read the registry" in done.output
