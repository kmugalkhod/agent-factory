"""Tests for factory_engine.gates.plan (task 2.16): the planner gate."""

from pathlib import Path

import pytest

from factory_engine.events import GateResult
from factory_engine.gates.plan import (
    GATE,
    Criterion,
    PlanCheck,
    ProblemKind,
    Question,
    SplitPart,
    check_plan,
    check_plan_text,
    is_measurable,
)

FIXTURES = Path(__file__).parent / "fixtures"
TEMPLATE = Path(__file__).resolve().parents[3] / "templates" / "plan.md"

# The sections of a good plan, in template order; `make` swaps or drops some of them.
GOOD: dict[str, str] = {
    "Context": "Add a `--json` flag to `factory status`. See intent.md.",
    "Past runs used": "None",
    "Files": (
        "- `cli/factory_cli/status.py`: change, add the flag\n"
        "- `cli/tests/test_status.py`: change, tests for the flag (tester)"
    ),
    "Steps": "1. Add the `--json` option.\n2. Print one JSON object per run.",
    "Acceptance criteria": "1. `factory status --json` exits 0 and prints a JSON list.",
    "Test cases": "- AC1: `test_status_json_parses`: two runs → a list of 2",
}


def make(changes: dict[str, str | None] | None = None) -> str:
    """A plan from GOOD with some sections replaced (a string), dropped (None) or added after
    the others (a heading GOOD doesn't have)."""
    sections: dict[str, str | None] = {**GOOD, **(changes or {})}
    parts = ["# Plan: run-42-status-json", ""]
    for heading, body in sections.items():
        if body is not None:
            parts += [f"## {heading}", "", body, ""]
    return "\n".join(parts)


def kinds(check: PlanCheck) -> tuple[ProblemKind, ...]:
    return tuple(problem.kind for problem in check.problems)


# ------------------------------------------------------------------ a good plan passes


def test_good_plan_passes() -> None:
    check = check_plan(FIXTURES / "good.md")
    assert check.passed
    assert check.problems == ()


def test_good_plan_returns_its_files() -> None:
    check = check_plan(FIXTURES / "good.md")
    assert check.files == ("cli/factory_cli/status.py", "cli/tests/test_status.py")


def test_good_plan_returns_its_steps() -> None:
    check = check_plan(FIXTURES / "good.md")
    assert len(check.steps) == 3
    assert check.steps[0].startswith("In `status.py`, add a `--json` option")


def test_good_plan_returns_its_criteria_all_measurable() -> None:
    check = check_plan(FIXTURES / "good.md")
    assert [c.number for c in check.criteria] == [1, 2, 3, 4]
    assert all(c.measurable for c in check.criteria)
    assert check.criteria[2] == Criterion(
        number=3, text="With no runs, `factory status --json` prints `[]`.", measurable=True
    )


def test_good_plan_reason_counts_what_it_found() -> None:
    check = check_plan(FIXTURES / "good.md")
    assert check.reason == (
        "plan.md names 2 files, 3 steps and 4 acceptance criteria, all measurable."
    )


def test_good_plan_without_split_or_questions_returns_none_for_both() -> None:
    check = check_plan(FIXTURES / "good.md")
    assert check.split is None
    assert check.questions is None


def test_passing_check_becomes_a_passing_gate_result_event() -> None:
    check = check_plan(FIXTURES / "good.md")
    assert check.event() == GateResult(gate=GATE, passed=True, reason=check.reason)


def test_windows_line_endings_and_a_byte_order_mark_still_pass(tmp_path: Path) -> None:
    path = tmp_path / "plan.md"
    path.write_bytes(b"\xef\xbb\xbf" + make().replace("\n", "\r\n").encode("utf-8"))
    assert check_plan(path).passed


def test_plan_with_one_measurable_criterion_among_vague_ones_passes() -> None:
    criteria = "1. It works well.\n2. `factory status --json` exits 0.\n3. Errors are clear."
    check = check_plan_text(make({"Acceptance criteria": criteria}))
    assert check.passed
    assert [c.measurable for c in check.criteria] == [False, True, False]


def test_passing_reason_names_the_criteria_that_arent_measurable() -> None:
    criteria = "1. It works well.\n2. `factory status --json` exits 0.\n3. Errors are clear."
    check = check_plan_text(make({"Acceptance criteria": criteria}))
    assert "1 measurable (not measurable: 1, 3)" in check.reason


# ------------------------------------------------------------------ each failure, its own reason

BAD = [
    ("no_files.md", ProblemKind.NO_FILES),
    ("no_steps.md", ProblemKind.NO_STEPS),
    ("no_criteria.md", ProblemKind.NO_CRITERIA),
    ("vague_criteria.md", ProblemKind.VAGUE_CRITERIA),
]


@pytest.mark.parametrize(("fixture", "kind"), BAD)
def test_bad_plan_fails_with_its_own_problem(fixture: str, kind: ProblemKind) -> None:
    check = check_plan(FIXTURES / fixture)
    assert not check.passed
    assert kinds(check) == (kind,)


@pytest.mark.parametrize(
    ("fixture", "heading"),
    [
        ("no_files.md", "`## Files`"),
        ("no_steps.md", "`## Steps`"),
        ("no_criteria.md", "`## Acceptance criteria`"),
        ("vague_criteria.md", "measurable"),
    ],
)
def test_bad_plan_reason_says_what_to_fix(fixture: str, heading: str) -> None:
    assert heading in check_plan(FIXTURES / fixture).reason


def test_each_bad_plan_has_a_distinct_reason() -> None:
    reasons = {check_plan(FIXTURES / fixture).reason for fixture, _ in BAD}
    assert len(reasons) == len(BAD)


@pytest.mark.parametrize(("fixture", "kind"), BAD)
def test_failing_check_becomes_a_failing_gate_result_event(fixture: str, kind: ProblemKind) -> None:
    check = check_plan(FIXTURES / fixture)
    assert check.event() == GateResult(gate=GATE, passed=False, reason=check.reason)


def test_vague_reason_lists_the_vague_criteria() -> None:
    assert "1, 2, 3" in check_plan(FIXTURES / "vague_criteria.md").reason


def test_missing_plan_file_fails(tmp_path: Path) -> None:
    check = check_plan(tmp_path / "plan.md")
    assert kinds(check) == (ProblemKind.MISSING_PLAN,)
    assert "plan.md" in check.reason


def test_plan_path_that_is_a_folder_fails(tmp_path: Path) -> None:
    (tmp_path / "plan.md").mkdir()
    assert kinds(check_plan(tmp_path / "plan.md")) == (ProblemKind.MISSING_PLAN,)


def test_unfilled_template_fails_on_files_steps_and_criteria() -> None:
    found = set(kinds(check_plan(TEMPLATE)))
    assert {ProblemKind.NO_FILES, ProblemKind.NO_STEPS, ProblemKind.NO_CRITERIA} <= found


@pytest.mark.parametrize(
    ("changes", "kind"),
    [
        ({"Files": "- cli/factory_cli/status.py: change, no backticks"}, ProblemKind.NO_FILES),
        ({"Files": "- `<path>`: <create | change>, <why>"}, ProblemKind.NO_FILES),
        ({"Files": "None"}, ProblemKind.NO_FILES),
        ({"Steps": "- Add the option.\n- Print the JSON."}, ProblemKind.NO_STEPS),
        ({"Steps": "1. <step>"}, ProblemKind.NO_STEPS),
        ({"Acceptance criteria": "- `factory status --json` exits 0."}, ProblemKind.NO_CRITERIA),
        ({"Acceptance criteria": "1. <criterion>"}, ProblemKind.NO_CRITERIA),
        ({"Acceptance criteria": "1. It works.\n2. It is fast."}, ProblemKind.VAGUE_CRITERIA),
    ],
)
def test_section_content_rule(changes: dict[str, str | None], kind: ProblemKind) -> None:
    assert kinds(check_plan_text(make(changes))) == (kind,)


def test_numbered_files_count_as_files() -> None:
    check = check_plan_text(make({"Files": "1. `src/a.py`: create, the module"}))
    assert check.passed
    assert check.files == ("src/a.py",)


def test_criterion_wrapped_onto_a_second_line_is_one_criterion() -> None:
    criteria = "1. Running the status command with the flag\n   exits 0 and prints `[]`."
    check = check_plan_text(make({"Acceptance criteria": criteria}))
    assert check.passed
    assert check.criteria == (
        Criterion(
            number=1,
            text="Running the status command with the flag exits 0 and prints `[]`.",
            measurable=True,
        ),
    )


def test_text_after_a_blank_line_doesnt_join_the_criterion() -> None:
    criteria = "1. The flag works well.\n\nSee `intent.md` for the command."
    check = check_plan_text(make({"Acceptance criteria": criteria}))
    assert kinds(check) == (ProblemKind.VAGUE_CRITERIA,)


def test_problems_are_all_reported_at_once() -> None:
    check = check_plan_text(make({"Files": "None", "Steps": "Later."}))
    assert kinds(check) == (ProblemKind.NO_FILES, ProblemKind.NO_STEPS)
    assert check.reason.count("\n") == 1


# ------------------------------------------------------------------ the measurable rule


@pytest.mark.parametrize(
    ("text", "measurable"),
    [
        ("`factory status --json` exits 0", True),  # a command in backticks
        ('`parse("")` raises an error', True),  # code in backticks
        ("Importing 10,000 rows finishes in under 5 seconds", True),  # a value
        ("Covered by test_slugify_empty", True),  # a test name
        ("The settings page shows an error when the email is empty", True),
        ("A row is written to the registry for each run", True),
        ("The command refuses a branch that isn't the run's own", True),
        ("The config file exists in the run folder afterwards", True),
        ("Works well", False),
        ("Is fast", False),
        ("Handles bad input gracefully", False),
        ("Errors are clear", False),
        ("The output is clean and easy to use", False),
        ("Logs are tidy and helpful", False),
        ("Supports Windows", False),
        ("The test suite is solid", False),
    ],
)
def test_is_measurable(text: str, measurable: bool) -> None:
    assert is_measurable(text) is measurable


# ------------------------------------------------------------------ headings are the contract


@pytest.mark.parametrize(
    "dropped", ["Context", "Past runs used", "Files", "Steps", "Acceptance criteria", "Test cases"]
)
def test_missing_required_heading_fails_on_headings(dropped: str) -> None:
    check = check_plan_text(make({dropped: None}))
    assert kinds(check) == (ProblemKind.HEADINGS,)
    assert f"`## {dropped}`" in check.reason


def test_unknown_heading_fails_on_headings() -> None:
    check = check_plan_text(make({"Risks": "None"}))
    assert kinds(check) == (ProblemKind.HEADINGS,)
    assert "`## Risks`" in check.reason


def test_repeated_heading_fails_on_headings() -> None:
    text = make() + "\n## Steps\n\n3. One more step.\n"
    check = check_plan_text(text)
    assert kinds(check) == (ProblemKind.HEADINGS,)
    assert "`## Steps` appears 2 times" in check.reason


def test_headings_out_of_order_fail_on_headings() -> None:
    order = ["Context", "Past runs used", "Steps", "Files", "Acceptance criteria", "Test cases"]
    text = "\n".join(f"## {heading}\n\n{GOOD[heading]}\n" for heading in order)
    check = check_plan_text(text)
    assert kinds(check) == (ProblemKind.HEADINGS,)
    assert "out of order" in check.reason


def test_heading_with_trailing_spaces_still_counts() -> None:
    assert check_plan_text(make().replace("## Files\n", "## Files   \n")).passed


def test_empty_plan_fails_on_headings() -> None:
    assert kinds(check_plan_text("")) == (ProblemKind.HEADINGS,)


def test_code_fence_fails() -> None:
    context = "Run this:\n\n```\nfactory status\n```"
    check = check_plan_text(make({"Context": context}))
    assert kinds(check) == (ProblemKind.CODE_FENCE,)


# ------------------------------------------------------------------ Split and Questions


def test_split_is_returned_as_parts() -> None:
    check = check_plan(FIXTURES / "split_questions.md")
    assert check.passed
    assert check.split == (
        SplitPart(
            number=1,
            title="Registry column",
            scope="add the `labels` column and its migration",
            depends_on=(),
        ),
        SplitPart(
            number=2,
            title="CLI flag",
            scope="`factory status --label` filters runs by label",
            depends_on=(1,),
        ),
        SplitPart(
            number=3,
            title="Board filter",
            scope="the runs board filters by label",
            depends_on=(1, 2),
        ),
    )


def test_questions_are_returned_with_why_and_default() -> None:
    check = check_plan(FIXTURES / "split_questions.md")
    assert check.questions == (
        Question(
            number=1,
            text="Should labels be case-sensitive?",
            why="it decides the column's collation.",
            default="case-insensitive",
        ),
        Question(number=2, text="Can a run have no labels at all?", why=None, default=None),
    )


def test_passing_reason_mentions_split_and_questions() -> None:
    reason = check_plan(FIXTURES / "split_questions.md").reason
    assert reason.endswith(" It proposes a split into 3 parts and asks 2 questions.")


def test_questions_without_split_are_returned_alone() -> None:
    questions = "1. Is the flag `--json` or `--format json`?"
    check = check_plan_text(make({"Questions": questions}))
    assert check.passed
    assert check.split is None
    assert check.questions == (
        Question(number=1, text="Is the flag `--json` or `--format json`?", why=None, default=None),
    )


def test_split_without_questions_is_returned_alone() -> None:
    split = "1. Flag: add the flag, depends on none\n2. Docs: document it, depends on part 1"
    check = check_plan_text(make({"Split": split}))
    assert check.passed
    assert check.questions is None
    assert check.split is not None
    assert [part.depends_on for part in check.split] == [(), (1,)]


@pytest.mark.parametrize(
    ("split", "phrase"),
    [
        ("To be decided.", "no numbered parts"),
        ("1. Everything: the whole change, depends on none", "at least two parts"),
        ("1. A: first, depends on none\n3. B: second, depends on part 1", "numbered 1, 2"),
        ("1. A: first, depends on none\n2. B: second", "doesn't say what it depends on"),
        ("1. A: first, depends on none\n2. B: second, depends on the parser", "names no part"),
        ("1. A: first, depends on part 2\n2. B: second, depends on none", "isn't an earlier part"),
        ("1. A: first, depends on none\n2. B: second, depends on part 2", "isn't an earlier part"),
    ],
)
def test_bad_split_fails_on_split(split: str, phrase: str) -> None:
    check = check_plan_text(make({"Split": split}))
    assert kinds(check) == (ProblemKind.SPLIT,)
    assert phrase in check.reason


def test_questions_heading_with_no_questions_fails_on_questions() -> None:
    check = check_plan_text(make({"Questions": "None."}))
    assert kinds(check) == (ProblemKind.QUESTIONS,)


def test_split_and_questions_are_returned_when_the_plan_fails() -> None:
    check = check_plan_text(make({"Files": "None", "Questions": "1. Which format?"}))
    assert not check.passed
    assert check.questions == (Question(number=1, text="Which format?", why=None, default=None),)
