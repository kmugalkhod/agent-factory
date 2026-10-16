"""The reviewer gate reads `review.md`: Important findings send the builder back, a malformed file
sends the reviewer back, and anything else passes."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from factory_engine.events import GateResult
from factory_engine.gates.review import (
    GATE,
    Finding,
    check_review,
    check_review_file,
    parse_finding,
    record_findings,
)
from factory_engine.run import Metrics, Run, new_run

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "review"
TEMPLATE = Path(__file__).resolve().parents[3] / "templates" / "review.md"

TITLE = "# Review: run-7-login-form\n\nAttempt: 1\nDiff reviewed: 1a2b3c4..5d6e7f8\n"
MET = (
    "- AC1: met. `tests/test_login.py::test_rejects_empty_password` passes.\n"
    "- AC2: met. `src/login.py:42` returns 401 for a wrong password."
)
IMPORTANT = "- [Important] `src/login.py:42`: A wrong password logs in. Fix: Return 401."
MINOR = "- [Minor] `src/login.py:30`: The message names the user. Fix: Drop the name."


def doc(*, criteria: str = MET, findings: str = "None", verdict: str = "Verdict: approve") -> str:
    """A filled `review.md` with the three sections in order."""
    return (
        f"{TITLE}\n## Acceptance criteria\n\n{criteria}\n\n## Findings\n\n{findings}\n\n"
        f"## Verdict\n\n{verdict}\n"
    )


def a_run() -> Run:
    return new_run(
        run_id=7,
        slug="login-form",
        repo="demo",
        kind="feature",
        provider="subscription",
        isolation="worktree",
        now=datetime(2026, 10, 14, tzinfo=UTC),
    )


# ------------------------------------------------------------------ pass


def test_the_helper_document_passes() -> None:
    """Guards the malformed table below: each case breaks only what it names."""
    assert check_review(doc()).passed


def test_no_findings_passes() -> None:
    outcome = check_review_file(FIXTURES / "approve-no-findings.md")
    assert (outcome.passed, outcome.important, outcome.retry) == (True, 0, None)


def test_minor_and_nit_findings_pass() -> None:
    outcome = check_review_file(FIXTURES / "approve-minor-findings.md")
    assert (outcome.passed, outcome.important, outcome.retry) == (True, 0, None)


def test_pass_reason_counts_the_other_findings() -> None:
    outcome = check_review_file(FIXTURES / "approve-minor-findings.md")
    assert outcome.reason == "no Important findings (1 Minor, 1 Nit)"


@pytest.mark.parametrize("none", ["None", "None.", "none"])
def test_none_in_any_case_means_no_findings(none: str) -> None:
    outcome = check_review(doc(findings=none))
    assert outcome.passed
    assert outcome.findings == ()


def test_windows_line_endings_parse() -> None:
    assert check_review(doc(findings=MINOR).replace("\n", "\r\n")).passed


def test_a_byte_order_mark_before_the_first_heading_is_ignored() -> None:
    text = doc().removeprefix(TITLE).lstrip("\n")
    assert check_review("\ufeff" + text).passed


def test_trailing_spaces_after_a_heading_are_ignored() -> None:
    assert check_review(doc().replace("## Findings\n", "## Findings  \n")).passed


# ------------------------------------------------------------------ Important findings


@pytest.mark.parametrize(
    ("fixture", "count"),
    [("request-changes-one-important.md", 1), ("request-changes-two-important.md", 2)],
)
def test_important_findings_fail_and_resume_the_builder(fixture: str, count: int) -> None:
    outcome = check_review_file(FIXTURES / fixture)
    assert (outcome.passed, outcome.important, outcome.retry) == (False, count, "builder")


@pytest.mark.parametrize(
    ("fixture", "words"),
    [
        ("request-changes-one-important.md", "1 Important finding;"),
        ("request-changes-two-important.md", "2 Important findings;"),
    ],
)
def test_important_failure_reason_names_the_count(fixture: str, words: str) -> None:
    assert words in check_review_file(FIXTURES / fixture).reason


def test_findings_are_returned_in_order() -> None:
    outcome = check_review_file(FIXTURES / "request-changes-one-important.md")
    assert outcome.findings == (
        Finding(
            severity="Important",
            path="src/login.py",
            line="42",
            problem="A wrong password logs the user in",
            fix="Compare the hash and return 401 on a mismatch.",
        ),
        Finding(
            severity="Nit",
            path="src/login.py",
            line="12",
            problem="`pw` is a vague name",
            fix="Rename it to `password`.",
        ),
    )


def test_the_verdict_is_returned() -> None:
    outcome = check_review_file(FIXTURES / "request-changes-two-important.md")
    assert outcome.verdict == "request changes"


def test_a_problem_may_contain_full_stops() -> None:
    finding = parse_finding("- [Minor] `src/api.py:3`: Calls v1.2 of the API. Fix: Call v2.")
    assert finding == Finding(
        severity="Minor",
        path="src/api.py",
        line="3",
        problem="Calls v1.2 of the API",
        fix="Call v2.",
    )


def test_the_template_example_findings_match_the_gate_format() -> None:
    examples = [
        line for line in TEMPLATE.read_text(encoding="utf-8").splitlines() if line.startswith("- [")
    ]
    parsed = [parse_finding(line) for line in examples]
    assert [f.severity for f in parsed if f is not None] == ["Important", "Minor", "Nit"]


# ------------------------------------------------------------------ malformed

HEADINGS_ONLY_TWO = f"{TITLE}\n## Acceptance criteria\n\n{MET}\n\n## Verdict\n\nVerdict: approve\n"
HEADINGS_OUT_OF_ORDER = (
    f"{TITLE}\n## Acceptance criteria\n\n{MET}\n\n## Verdict\n\nVerdict: approve\n\n"
    "## Findings\n\nNone\n"
)

MALFORMED = [
    pytest.param("", "headings", id="empty"),
    pytest.param(TITLE, "headings", id="title only"),
    pytest.param(HEADINGS_ONLY_TWO, "headings", id="no Findings heading"),
    pytest.param(HEADINGS_OUT_OF_ORDER, "headings", id="headings out of order"),
    pytest.param(doc() + "\n## Notes\n\nNothing.\n", "headings", id="extra heading"),
    pytest.param(doc(findings="None\n\n## Findings\n\nNone"), "headings", id="repeated heading"),
    pytest.param(
        doc(findings="- [Major] `src/a.py:3`: Off by one. Fix: Use <=."),
        "isn't a finding",
        id="unknown severity",
    ),
    pytest.param(
        doc(findings="- [important] `src/a.py:3`: Off by one. Fix: Use <=."),
        "isn't a finding",
        id="lower-case severity",
    ),
    pytest.param(
        doc(findings="- [Important] `src/a.py:3`: Off by one."),
        "isn't a finding",
        id="no fix",
    ),
    pytest.param(
        doc(findings="- [Important] src/a.py:3: Off by one. Fix: Use <=."),
        "isn't a finding",
        id="location not in backticks",
    ),
    pytest.param(
        doc(findings="- [Important] `src/a.py`: Off by one. Fix: Use <=."),
        "isn't a finding",
        id="location without a line",
    ),
    pytest.param(doc(findings="The code looks fine."), "isn't a finding", id="prose in findings"),
    pytest.param(doc(findings=""), "Findings section is empty", id="empty findings"),
    pytest.param(
        doc(findings=f"None\n{MINOR}"), '"None" next to findings', id="None and a finding"
    ),
    pytest.param(doc(verdict=""), "Verdict section", id="no verdict"),
    pytest.param(
        doc(verdict="Verdict: approve\nVerdict: request changes"),
        "Verdict section",
        id="two verdicts",
    ),
    pytest.param(doc(verdict="Verdict: lgtm"), "Verdict section", id="unknown verdict"),
    pytest.param(doc(verdict="Verdict: Approve"), "Verdict section", id="verdict in capitals"),
    pytest.param(
        doc(verdict="Verdict: approve\nLooks good to me."),
        "Verdict section",
        id="verdict with prose",
    ),
    pytest.param(
        doc(findings=IMPORTANT, verdict="Verdict: approve"),
        "the verdict is approve but 1 finding is Important",
        id="approve with an Important finding",
    ),
    pytest.param(
        doc(findings=MINOR, verdict="Verdict: request changes"),
        "no finding is Important",
        id="request changes with no Important finding",
    ),
    pytest.param(
        doc(criteria="- AC1: met. Tests pass.\n- AC2: not met. 200 on a wrong password."),
        "the verdict is approve but 1 criterion is not met",
        id="approve with a criterion not met",
    ),
    pytest.param(doc(criteria="All good."), "marks no criterion", id="no criterion marked"),
    pytest.param(
        doc(criteria="- AC1: met. Tests pass.\n- AC2: partly met. Half the cases."),
        "doesn't say `met` or `not met`",
        id="unclear criterion",
    ),
    pytest.param(
        doc(criteria="- AC1: met | not met. Evidence pending."),
        "doesn't say `met` or `not met`",
        id="criterion with both statuses",
    ),
    pytest.param(
        doc(criteria="- AC1: met, mostly. Tests pass."),
        "doesn't say `met` or `not met`",
        id="criterion status with more words",
    ),
    pytest.param(doc(criteria="- AC1: met"), "then its evidence", id="criterion without evidence"),
    pytest.param(
        doc(criteria="- AC1: met."), "then its evidence", id="criterion with empty evidence"
    ),
    pytest.param(
        doc(criteria="- AC1: met Tests pass."),
        "then its evidence",
        id="criterion without the full stop",
    ),
]


@pytest.mark.parametrize(("text", "words"), MALFORMED)
def test_malformed_review_fails_and_retries_the_reviewer(text: str, words: str) -> None:
    outcome = check_review(text)
    assert (outcome.passed, outcome.retry, outcome.important) == (False, "reviewer", 0)


@pytest.mark.parametrize(("text", "words"), MALFORMED)
def test_malformed_review_reason_says_what_is_wrong(text: str, words: str) -> None:
    assert words in check_review(text).reason


def test_malformed_reason_names_the_bad_line() -> None:
    text = doc(findings="The code looks fine.")
    number = text.splitlines().index("The code looks fine.") + 1
    assert f"line {number} " in check_review(text).reason


def test_the_unfilled_template_is_malformed() -> None:
    outcome = check_review_file(TEMPLATE)
    assert (outcome.passed, outcome.retry) == (False, "reviewer")


def test_a_missing_review_retries_the_reviewer(tmp_path: Path) -> None:
    outcome = check_review_file(tmp_path / "review.md")
    assert (outcome.passed, outcome.retry) == (False, "reviewer")
    assert "can't be read" in outcome.reason


def test_a_review_that_is_not_utf8_retries_the_reviewer(tmp_path: Path) -> None:
    path = tmp_path / "review.md"
    path.write_bytes(doc().encode("utf-8") + b"\xff\xfe")
    outcome = check_review_file(path)
    assert (outcome.passed, outcome.retry) == (False, "reviewer")


# ------------------------------------------------------------------ event and metrics


@pytest.mark.parametrize("fixture", ["approve-no-findings.md", "request-changes-two-important.md"])
def test_the_event_carries_the_outcome(fixture: str) -> None:
    outcome = check_review_file(FIXTURES / fixture)
    assert outcome.event() == GateResult(gate=GATE, passed=outcome.passed, reason=outcome.reason)


def test_the_gate_is_named_review() -> None:
    assert GATE == "review"


def test_record_findings_adds_the_count_to_the_run() -> None:
    run = a_run().model_copy(update={"metrics": Metrics(important_findings=1)})
    outcome = check_review_file(FIXTURES / "request-changes-two-important.md")
    assert record_findings(run, outcome).metrics.important_findings == 3


def test_record_findings_leaves_the_given_run_unchanged() -> None:
    run = a_run()
    record_findings(run, check_review_file(FIXTURES / "request-changes-two-important.md"))
    assert run.metrics.important_findings == 0


def test_a_malformed_review_records_no_findings() -> None:
    outcome = check_review(doc(findings=IMPORTANT, verdict="Verdict: approve"))
    assert record_findings(a_run(), outcome).metrics.important_findings == 0
