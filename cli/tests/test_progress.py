"""Tests for the progress lines `start`, `approve` and `resume` print while a run works: one
short line per meaningful event, tool calls and agent messages only with --verbose."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from factory_cli.progress import MAX_TEXT, Progress, progress_line
from factory_engine import events as ev
from factory_engine.events import Body, Event
from factory_engine.run import Role

AT = datetime(2026, 10, 17, 9, 5, 7, tzinfo=UTC)


def event(body: Body, role: Role | None = None, at: datetime = AT) -> Event:
    return Event(seq=1, at=at, repo="pilot", run_id=3, role=role, body=body)


@pytest.mark.parametrize(
    ("body", "role", "line"),
    [
        (ev.RunCreated(kind="feature"), None, "09:05:07 run created (feature)"),
        (
            ev.StateChanged(from_state="pending", to_state="planning"),
            None,
            "09:05:07 state pending -> planning",
        ),
        (
            ev.StageStarted(stage="planner", attempt=1),
            "planner",
            "09:05:07 [planner] started attempt 1",
        ),
        (
            ev.StageFinished(stage="builder", attempt=2, outcome="failed"),
            "builder",
            "09:05:07 [builder] attempt 2 failed",
        ),
        (
            ev.GateResult(gate="plan", passed=True, reason="ok"),
            "planner",
            "09:05:07 [planner] gate plan passed",
        ),
        (
            ev.GateResult(gate="review", passed=False, reason="2 Important findings"),
            "reviewer",
            "09:05:07 [reviewer] gate review failed: 2 Important findings",
        ),
        (
            ev.TestRun(command="uv run pytest", exit_code=1, passed=3, failed=2, seconds=4.5),
            "tester",
            "09:05:07 [tester] tests `uv run pytest`: 3 passed, 2 failed (exit 1, 4.5s)",
        ),
        (
            ev.TestRun(command="make test", exit_code=0, passed=None, failed=None, seconds=12),
            "builder",
            "09:05:07 [builder] tests `make test`: exit 0 (12.0s)",
        ),
        (
            ev.NeedsYou(reason="builder cap reached"),
            None,
            "09:05:07 needs you: builder cap reached",
        ),
        (
            ev.Paused(reason="turn_cap", detail="the builder used its 200 turns", resume_at=None),
            "builder",
            "09:05:07 [builder] paused (turn_cap): the builder used its 200 turns",
        ),
        (
            ev.Paused(
                reason="usage_limit", detail="usage limit", resume_at=AT + timedelta(hours=2)
            ),
            "builder",
            "09:05:07 [builder] paused (usage_limit): usage limit; resumes at 11:05:07",
        ),
        (ev.Decision(action="approve_plan"), None, "09:05:07 your decision: approve_plan"),
        (
            ev.ToolCall(tool="Bash", target="git push", decision="blocked", reason="git"),
            "builder",
            "09:05:07 [builder] blocked Bash git push: git",
        ),
        (
            ev.StateChanged(from_state="reviewing", to_state="awaiting_merge"),
            None,
            "09:05:07 state reviewing -> awaiting_merge (the pull request is open)",
        ),
    ],
)
def test_meaningful_events_get_one_line(body: Body, role: Role | None, line: str) -> None:
    assert progress_line(event(body, role), verbose=False, tz=UTC) == line


@pytest.mark.parametrize(
    ("body", "line"),
    [
        (
            ev.ToolCall(tool="Write", target="plan.md", decision="allowed", reason=None),
            "09:05:07 [planner] Write plan.md",
        ),
        (ev.SkillLoaded(skill="write-plan"), "09:05:07 [planner] skill write-plan"),
        (
            ev.Tokens(attempt=1, input=1200, output=300),
            "09:05:07 [planner] tokens 1,200 in, 300 out",
        ),
        (
            ev.Message(sender="agent", text="Plan written."),
            "09:05:07 [planner] agent: Plan written.",
        ),
        (
            ev.Message(sender="you", text="Use the helper."),
            "09:05:07 [planner] you: Use the helper.",
        ),
    ],
)
def test_tool_calls_and_messages_show_only_when_verbose(body: Body, line: str) -> None:
    assert progress_line(event(body, "planner"), verbose=False, tz=UTC) is None
    assert progress_line(event(body, "planner"), verbose=True, tz=UTC) == line


def test_the_time_is_shown_in_the_given_zone() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))

    line = progress_line(event(ev.NeedsYou(reason="x")), verbose=False, tz=ist)

    assert line == "14:35:07 needs you: x"


def test_a_long_reason_is_cut_to_one_short_line() -> None:
    reason = "first line\n\n  second   line " + "x" * 500

    line = progress_line(event(ev.NeedsYou(reason=reason)), verbose=False, tz=UTC)

    assert line is not None
    text = line.removeprefix("09:05:07 needs you: ")
    assert text.startswith("first line second line x")
    assert text.endswith("...")
    assert len(text) == MAX_TEXT
    assert "\n" not in line


def test_control_characters_in_agent_text_are_not_printed() -> None:
    body = ev.Message(sender="agent", text="\x1b[31mred\x1b[0m\tdone\x07")

    line = progress_line(event(body, "builder"), verbose=True, tz=UTC)

    assert line is not None
    assert line.isprintable()
    assert "red" in line and "done" in line


def test_progress_writes_a_line_per_shown_event_and_skips_the_rest() -> None:
    lines: list[str] = []
    progress = Progress(lines.append, verbose=False, tz=UTC)

    progress(event(ev.StageStarted(stage="planner", attempt=1), "planner"))
    progress(event(ev.ToolCall(tool="Read", target="a.py", decision="allowed", reason=None)))

    assert lines == ["09:05:07 [planner] started attempt 1"]
