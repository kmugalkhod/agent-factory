"""Tests for factory_engine.report (task 2.27): `report.md`, written from the template into the
run's folder when the run ends or waits for you, at most one page."""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from factory_engine import events as ev
from factory_engine.config import Settings, default_settings
from factory_engine.events import EventLog, NeedsYou
from factory_engine.gates.tests_gate import record_tester_cases
from factory_engine.intake import frame_request
from factory_engine.metrics import builder_gate, review_verdict, token_use
from factory_engine.providers import get_profile, resolve_model
from factory_engine.report import (
    HEADINGS,
    MAX_LINES,
    REPORT_FILE,
    render_report,
    reported,
    write_report,
)
from factory_engine.run import Metrics, PullRequestInfo, Role, Run, RunState, new_run

START = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
TEMPLATE = Path(__file__).resolve().parents[2] / "templates" / "report.md"
PR_URL = "https://github.com/kunal/pilot/pull/7"

PLAN = """# Plan: add mul

## Context

`src/calc.py` has `add`; the request asks for `mul`.

## Past runs used

None

## Files

- `src/calc.py`: add `mul`

## Steps

1. Add `mul(a, b)` to `src/calc.py`.

## Acceptance criteria

1. `mul(2, 3)` returns 6.
2. `mul` has a docstring.

## Test cases

- AC1: `test_mul`: `mul(2, 3)` is 6
- AC2: `test_mul_doc`: `mul.__doc__` is set
"""
REVIEW = """# Review

## Acceptance criteria

- AC1: met. `tests/test_mul.py::test_mul` passes.
- AC2: met. `tests/test_mul.py::test_mul_doc` passes.

## Findings

- [Minor] `src/calc.py:5`: the docstring doesn't name the arguments. Fix: Name them.

## Verdict

Verdict: approve
"""
HANDOFF = """# Handoff: builder

## Files touched

- `src/calc.py`: changed, added `mul`
- `src/util.py`: created, a helper

## Decisions

None

## Rejected options

None

## Gaps

- Floats aren't tested.
"""


def headings(text: str) -> list[str]:
    return [line[3:].strip() for line in text.splitlines() if line.startswith("## ")]


def _write(folder: Path, files: dict[str, str | bytes]) -> None:
    for name, text in files.items():
        data = text.encode("utf-8") if isinstance(text, str) else text
        (folder / name).write_bytes(data)


def _metrics() -> Metrics:
    metrics = Metrics(important_findings=1)
    uses: list[tuple[Role, int, int]] = [
        ("planner", 1, 100),
        ("tester", 1, 200),
        ("builder", 1, 300),
        ("builder", 2, 400),
        ("reviewer", 1, 50),
    ]
    for role, attempt, count in uses:
        metrics = token_use(metrics, role=role, attempt=attempt, at=START, input=count, output=0)
    metrics = builder_gate(metrics, True)
    metrics = review_verdict(metrics, False)
    return metrics.model_copy(update={"builder_retries": 1, "pr_lines_changed_by_you": 4})


def _run(state: RunState = "merged", **changes: object) -> Run:
    run = new_run(
        run_id=4,
        slug="add-mul",
        repo="pilot",
        kind="feature",
        provider="subscription",
        isolation="worktree",
        now=START,
    )
    update: dict[str, object] = {
        "state": state,
        "stage": "reviewer",
        "attempts": {"planner": 1, "tester": 1, "builder": 2, "reviewer": 2},
        "updated_at": START + timedelta(minutes=42),
        "metrics": _metrics(),
        "pull_request": PullRequestInfo(number=7, url=PR_URL, head="a" * 40, opened_head="b" * 40),
    }
    update.update(changes)
    return run.model_copy(update=update)


@pytest.fixture
def settings() -> Settings:
    return default_settings(sys.platform)


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    """A run folder as a finished run leaves it."""
    run_folder = tmp_path / "run"
    run_folder.mkdir()
    intent = b"# Intent: run-4-add-mul\n\n" + frame_request(b"Add `mul(a, b)` to calc.\n\nMore.")
    _write(
        run_folder,
        {"intent.md": intent, "plan.md": PLAN, "review.md": REVIEW, "handoff-builder.md": HANDOFF},
    )
    record_tester_cases(run_folder, ["tests.test_mul::test_mul", "tests.test_mul::test_mul_doc"])
    events = EventLog(run_folder, repo="pilot", run_id=4, clock=lambda: START)
    events.append(
        ev.TestRun(command="uv run pytest", exit_code=1, passed=3, failed=2, seconds=1.0),
        role="tester",
    )
    events.append(
        ev.TestRun(command="uv run pytest", exit_code=0, passed=5, failed=0, seconds=1.0),
        role="builder",
    )
    return run_folder


def _render(run: Run, folder: Path, settings: Settings | None) -> str:
    profile = get_profile(run.provider) if settings is not None else None
    return render_report(run, folder, settings=settings, profile=profile)


def test_the_report_has_the_templates_headings_in_order(folder: Path, settings: Settings) -> None:
    text = _render(_run(), folder, settings)
    assert headings(text) == headings(TEMPLATE.read_text(encoding="utf-8"))
    assert headings(text) == list(HEADINGS)
    lines = text.splitlines()
    assert lines[0] == "# Report: run-4-add-mul"
    assert "Status: merged" in lines
    assert f"Pull request: {PR_URL}" in lines


def test_a_merged_run_reports_its_criteria_tests_review_and_metrics(
    folder: Path, settings: Settings
) -> None:
    text = _render(_run(), folder, settings)
    lines = text.splitlines()
    assert "- AC1: met, `test_mul`" in lines
    assert "- AC2: met, `test_mul_doc`" in lines
    assert "- Command: `uv run pytest`" in lines
    assert "- Result: passed, 5 passed, 0 failed, 2 added by the tester" in lines
    assert "- Verdict: approve" in lines
    assert "- Open findings: 1" in lines
    assert any(
        line.strip().startswith("- [Minor] `src/calc.py:5`: the docstring") for line in lines
    )
    profile = get_profile("subscription")
    models = ", ".join(
        f"{role} {resolve_model(profile, getattr(settings.roles, role).model)}"
        for role in ("planner", "tester", "builder", "reviewer")
    )
    assert f"- Provider: subscription; models: {models}" in lines
    assert "- Isolation: worktree" in lines
    assert (
        "- Builder attempts: 2; tokens: 1050 (planner 100, tester 200, builder 700, reviewer 50)"
        in lines
    )
    assert (
        "- Tokens per attempt: planner 1: 100, tester 1: 200, builder 1: 300, builder 2: 400, "
        "reviewer 1: 50" in lines
    )
    assert (
        "- First pass: no; builder retries: 1; Important findings: 1; usage limit pauses: 0; "
        "lines you changed on the pull request: 4" in lines
    )
    assert "- Duration: 2026-10-17 12:00 UTC to 2026-10-17 12:42 UTC" in lines
    assert "- Floats aren't tested." in lines
    summary = lines[lines.index("## Summary") + 2]
    assert "Add `mul(a, b)` to calc." in summary
    assert "2 files" in summary


@pytest.mark.parametrize(
    ("state", "status"),
    [
        ("merged", "merged"),
        ("awaiting_merge", "awaiting merge"),
        ("needs_you", "needs you"),
        ("failed", "failed"),
        ("stopped", "stopped"),
    ],
)
def test_the_status_line_follows_the_run(
    folder: Path, settings: Settings, state: RunState, status: str
) -> None:
    assert f"Status: {status}" in _render(_run(state), folder, settings).splitlines()


def test_a_run_that_needs_you_says_why_in_the_summary(folder: Path, settings: Settings) -> None:
    events = EventLog(folder, repo="pilot", run_id=4, clock=lambda: START)
    events.append(NeedsYou(reason="the builder reached its cap of 3 attempts."), role="builder")
    text = _render(_run("needs_you", pull_request=None), folder, settings)
    lines = text.splitlines()
    assert "Pull request: none" in lines
    action = lines[lines.index("## Summary") + 3]  # the line after what was asked and built
    assert action == "It needs you: the builder reached its cap of 3 attempts."


def test_a_long_request_never_cuts_why_the_run_needs_you(folder: Path, settings: Settings) -> None:
    _write(folder, {"intent.md": b"# Intent\n\n" + frame_request(("ask " * 300).encode())})
    reason = "the builder reached its cap of 3 attempts. " + "detail " * 200
    events = EventLog(folder, repo="pilot", run_id=4, clock=lambda: START)
    events.append(NeedsYou(reason=reason), role="builder")
    lines = _render(_run("needs_you"), folder, settings).splitlines()
    [action] = [line for line in lines if line.startswith("It needs you: ")]
    assert action.startswith("It needs you: the builder reached its cap of 3 attempts.")
    assert action.endswith("(the full reason is in events.jsonl)")
    assert len(action) <= 300
    assert lines.index(action) == lines.index("## Summary") + 3  # its own line, after the ask


def test_a_short_reason_is_given_whole(folder: Path, settings: Settings) -> None:
    events = EventLog(folder, repo="pilot", run_id=4, clock=lambda: START)
    events.append(NeedsYou(reason="the tests fail after the rebase."), role="builder")
    lines = _render(_run("needs_you"), folder, settings).splitlines()
    assert "It needs you: the tests fail after the rebase." in lines


def test_criteria_only_the_review_judged_are_listed_too(folder: Path, settings: Settings) -> None:
    """The review also judges intent.md's requirements that the plan doesn't cover."""
    extra = "- AC3: not met. The spec's CSV export is missing.\n"
    review = REVIEW.replace("\n## Findings", f"{extra}\n## Findings")
    assert extra in review
    _write(folder, {"review.md": review})
    lines = _render(_run("needs_you"), folder, settings).splitlines()
    assert "- AC3: not met, judged in review.md (not in plan.md)" in lines
    (folder / "plan.md").unlink()
    lines = _render(_run("needs_you"), folder, settings).splitlines()
    assert "- AC1: met, judged in review.md (not in plan.md)" in lines


@pytest.mark.parametrize("state", ["merged", "awaiting_merge", "needs_you", "failed", "stopped"])
def test_these_states_are_reported(state: RunState) -> None:
    assert reported(state)


@pytest.mark.parametrize("state", ["pending", "planning", "building", "paused", "closed"])
def test_working_or_paused_runs_get_no_report(state: RunState) -> None:
    assert not reported(state)


def test_a_run_that_ended_before_any_stage_still_follows_the_template(
    tmp_path: Path, settings: Settings
) -> None:
    """Intake failed: only `intent.md` exists, no plan, tests, review or tokens."""
    folder = tmp_path / "run"
    folder.mkdir()
    _write(folder, {"intent.md": b"# Intent\n\n" + frame_request(b"Add mul.")})
    run = _run("failed", stage=None, attempts={}, metrics=Metrics(), pull_request=None)
    text = _render(run, folder, settings)
    lines = text.splitlines()
    assert headings(text) == list(HEADINGS)
    assert "- Result: not run" in lines
    assert "- Verdict: none (no review)" in lines
    assert any(line.startswith("- First pass: not decided") for line in lines)
    assert "lines you changed on the pull request: not measured" in text
    assert len(lines) <= MAX_LINES


def test_without_settings_the_models_are_unknown(folder: Path) -> None:
    text = _render(_run("stopped"), folder, None)
    assert "- Provider: subscription; models: unknown" in text.splitlines()


def test_criteria_not_met_and_unreviewed_show_as_such(folder: Path, settings: Settings) -> None:
    review = (
        REVIEW.replace("- AC2: met.", "- AC2: not met.")
        .replace("- [Minor]", "- [Important]")
        .replace("Verdict: approve", "Verdict: request changes")
    )
    _write(folder, {"review.md": review})
    lines = _render(_run("needs_you"), folder, settings).splitlines()
    assert "- AC2: not met, `test_mul_doc`" in lines
    assert "- Verdict: request changes" in lines
    (folder / "review.md").unlink()
    lines = _render(_run("needs_you"), folder, settings).splitlines()
    assert "- AC1: not reviewed, `test_mul`" in lines


def test_a_huge_run_still_fits_on_one_page(folder: Path, settings: Settings) -> None:
    criteria = "".join(f"{n}. Value {n} is returned.\n" for n in range(1, 41))
    cases = "".join(f"- AC{n}: `test_{n}`: {n}\n" for n in range(1, 41))
    plan = PLAN.split("## Acceptance criteria")[0] + (
        f"## Acceptance criteria\n\n{criteria}\n## Test cases\n\n{cases}"
    )
    judged = "".join(f"- AC{n}: met. test_{n} passes.\n" for n in range(1, 41))
    findings = "".join(f"- [Minor] `src/a.py:{n}`: issue {n}. Fix: fix it.\n" for n in range(30))
    review = f"# Review\n\n## Acceptance criteria\n\n{judged}\n## Findings\n\n{findings}\n"
    review += "## Verdict\n\nVerdict: approve\n"
    gaps = "".join(f"- gap {n}\n" for n in range(30))
    long_request = ("word " * 2000 + "\n## Injected heading\n```\ncode\n```\n").encode()
    _write(
        folder,
        {
            "plan.md": plan,
            "review.md": review,
            "handoff-builder.md": HANDOFF.replace("- Floats aren't tested.\n", gaps),
            "intent.md": b"# Intent\n\n" + frame_request(long_request),
        },
    )
    metrics = _metrics()
    for attempt in range(1, 40):
        metrics = token_use(metrics, role="builder", attempt=attempt, at=START, input=1, output=1)
    text = _render(_run("needs_you", metrics=metrics), folder, settings)
    lines = text.splitlines()
    assert len(lines) <= MAX_LINES
    assert headings(text) == list(HEADINGS)
    assert not any(line.startswith("```") for line in lines)
    assert all(len(line) <= 400 for line in lines)
    assert any("more findings in review.md" in line for line in lines)
    assert any("more criteria in plan.md" in line for line in lines)


def test_write_report_puts_report_md_in_the_run_folder(folder: Path, settings: Settings) -> None:
    run = _run()
    path = write_report(run, folder, settings=settings, profile=get_profile(run.provider))
    assert path == folder / REPORT_FILE
    assert path.read_text(encoding="utf-8") == _render(run, folder, settings)
    assert not [p for p in folder.iterdir() if p.name.endswith(".tmp")]
