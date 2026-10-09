"""The default role prompts must match the design's "Settings per role" table word for word."""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PROMPTS = REPO / "prompts"
DESIGN = REPO / "docs" / "design" / "design.md"

ROLES = ("planner", "tester", "builder", "reviewer")
OUTPUT = {
    "planner": "plan.md",
    "tester": "handoff-tester.md",
    "builder": "handoff-builder.md",
    "reviewer": "review.md",
}
LIMIT_ROWS = ("May write", "May not touch", "Shell commands")


def role_table() -> dict[str, dict[str, str]]:
    """Parse the design's "Settings per role" table into {row name: {role: cell}}."""
    text = DESIGN.read_text(encoding="utf-8").splitlines()
    start = text.index("**Settings per role**")
    first = start + next(i for i, line in enumerate(text[start:]) if line.startswith("|"))
    rows: list[list[str]] = []
    for line in text[first:]:
        if not line.startswith("|"):
            break
        rows.append([cell.strip() for cell in line.strip("|").split("|")])
    header = [cell.lower() for cell in rows[0]]
    return {row[0]: dict(zip(header[1:], row[1:], strict=True)) for row in rows[2:]}


def prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


def prompt_lines(name: str) -> list[str]:
    return [line.strip() for line in prompt(name).splitlines()]


def test_design_table_has_the_rows_the_prompts_copy() -> None:
    table = role_table()
    for row in LIMIT_ROWS:
        assert set(table[row]) == set(ROLES)


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("row", LIMIT_ROWS)
def test_prompt_copies_the_design_table_exactly(role: str, row: str) -> None:
    expected = f"{row}: {role_table()[row][role]}"
    assert expected in prompt_lines(role)


@pytest.mark.parametrize("role", ROLES)
def test_prompt_names_its_output_file(role: str) -> None:
    assert f"`{OUTPUT[role]}`" in prompt(role)


@pytest.mark.parametrize("role", ROLES)
def test_prompt_mentions_handoff(role: str) -> None:
    assert "handoff" in prompt(role)


@pytest.mark.parametrize("role", ["planner", "tester", "builder"])
def test_prompt_names_its_own_handoff_file(role: str) -> None:
    assert f"`handoff-{role}.md`" in prompt(role)


def test_reviewer_writes_no_handoff_of_its_own() -> None:
    assert "handoff-reviewer.md" not in prompt("reviewer")


@pytest.mark.parametrize("role", ROLES)
def test_prompt_reads_the_handoffs_of_every_earlier_role(role: str) -> None:
    earlier = ROLES[: ROLES.index(role)]
    for other in earlier:
        if other != "reviewer":
            assert f"`handoff-{other}.md`" in prompt(role)


@pytest.mark.parametrize("role", ROLES)
def test_prompt_reads_intent(role: str) -> None:
    assert "`intent.md`" in prompt(role)


@pytest.mark.parametrize("role", ROLES)
def test_prompt_leaves_push_rebase_and_merge_to_the_engine(role: str) -> None:
    text = prompt(role)
    for command in ("git push", "rebase", "merge"):
        assert command in text


def test_tester_requires_tests_that_fail_before_the_build() -> None:
    assert "fail before the build" in prompt("tester")


def test_tester_writes_a_test_for_every_acceptance_criterion() -> None:
    assert "every acceptance criterion" in prompt("tester")


def test_builder_forbids_editing_tests() -> None:
    text = prompt("builder")
    assert re.search(r"Never edit, weaken, skip or delete a test", text)


def test_builder_runs_the_tests_before_finishing() -> None:
    assert "test command" in prompt("builder")


def test_reviewer_checks_the_diff_against_intent_and_plan() -> None:
    text = prompt("reviewer")
    assert re.search(r"diff against `intent\.md` and `plan\.md`", text)


def test_reviewer_uses_the_severities_and_verdict() -> None:
    text = prompt("reviewer")
    for word in ("Important", "Minor", "Nit", "Verdict: approve", "Verdict: request changes"):
        assert word in text


def test_planner_asks_questions_only_when_something_is_unclear() -> None:
    assert "only when something is unclear" in prompt("planner")


def test_planner_proposes_a_split_above_400_lines_or_10_files() -> None:
    text = prompt("planner")
    assert "400 changed lines" in text
    assert "10 files" in text
    assert "Split" in text


def test_planner_lists_the_past_runs_it_used() -> None:
    assert "Past runs used" in prompt("planner")


def test_bootstrap_addendum_covers_planner_and_builder() -> None:
    text = prompt("bootstrap")
    for needle in ("## Planner", "## Builder", "`CLAUDE.md`", "`factory.yaml`", "smoke test"):
        assert needle in text


@pytest.mark.parametrize("name", [*ROLES, "bootstrap"])
def test_prompt_is_short(name: str) -> None:
    assert len(prompt(name).splitlines()) <= 80


def test_no_other_prompts_exist() -> None:
    names = sorted(p.stem for p in PROMPTS.glob("*.md"))
    assert names == sorted([*ROLES, "bootstrap"])


def bootstrap_builder_section() -> str:
    text = prompt("bootstrap")
    return text[text.index("## Builder") :]


@pytest.mark.parametrize("missing", ["`handoff-tester.md`", "the tests the tester wrote"])
def test_bootstrap_builder_skips_reads_of_tester_output(missing: str) -> None:
    """Run 0 has no tester stage; the addendum must exempt the base prompt's reads."""
    assert missing in bootstrap_builder_section()
    assert missing.lower() in prompt("builder").lower()  # the read being exempted exists
