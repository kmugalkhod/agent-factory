"""The template headings are a contract with the gates; templates/README.md is its single list."""

import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[2] / "templates"
README = TEMPLATES / "README.md"
NAMES = ("intent.md", "plan.md", "handoff.md", "review.md", "report.md")
MAX_LINES = 60  # one page

SECTION = re.compile(r"^## (\S+\.md)$")
ITEM = re.compile(r"^- `([^`]+)`( \(optional\))?$")


def contract() -> dict[str, list[tuple[str, bool]]]:
    """Read README.md: for each template, its headings in order and whether each is optional."""
    result: dict[str, list[tuple[str, bool]]] = {}
    current: str | None = None
    for line in README.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            match = SECTION.match(line)
            current = match.group(1) if match else None
            if current is not None:
                result[current] = []
        elif current is not None and (item := ITEM.match(line)):
            result[current].append((item.group(1), item.group(2) is not None))
    return result


def headings(name: str) -> list[str]:
    """The level-2 headings of a template, in order."""
    text = (TEMPLATES / name).read_text(encoding="utf-8")
    return [line[3:].strip() for line in text.splitlines() if line.startswith("## ")]


def lines(name: str) -> list[str]:
    return (TEMPLATES / name).read_text(encoding="utf-8").splitlines()


def test_readme_lists_exactly_the_five_templates() -> None:
    assert sorted(contract()) == sorted(NAMES)


def test_every_listed_template_exists_and_no_other_does() -> None:
    on_disk = sorted(p.name for p in TEMPLATES.glob("*.md") if p.name != "README.md")
    assert on_disk == sorted(NAMES)


@pytest.mark.parametrize("name", NAMES)
def test_template_has_exactly_the_listed_headings_in_order(name: str) -> None:
    assert headings(name) == [heading for heading, _ in contract()[name]]


@pytest.mark.parametrize("name", NAMES)
def test_template_has_one_title(name: str) -> None:
    titles = [line for line in lines(name) if line.startswith("# ")]
    assert len(titles) == 1
    assert lines(name)[0] == titles[0]


@pytest.mark.parametrize("name", NAMES)
def test_template_is_at_most_one_page(name: str) -> None:
    assert len(lines(name)) <= MAX_LINES


@pytest.mark.parametrize("name", NAMES)
def test_template_has_no_code_fences(name: str) -> None:
    """Gates find headings by line; a fenced `## ` line would be misread."""
    assert not any(line.lstrip().startswith("```") for line in lines(name))


def test_plan_sections_match_the_acceptance_criteria() -> None:
    assert contract()["plan.md"] == [
        ("Context", False),
        ("Past runs used", False),
        ("Files", False),
        ("Steps", False),
        ("Acceptance criteria", False),
        ("Test cases", False),
        ("Split", True),
        ("Questions", True),
    ]


def test_handoff_sections_match_the_acceptance_criteria() -> None:
    assert contract()["handoff.md"] == [
        ("Files touched", False),
        ("Decisions", False),
        ("Rejected options", False),
        ("Gaps", False),
    ]


def test_review_has_findings_and_verdict_sections() -> None:
    sections = [heading for heading, _ in contract()["review.md"]]
    assert "Findings" in sections
    assert sections[-1] == "Verdict"


@pytest.mark.parametrize("severity", ["Important", "Minor", "Nit"])
def test_review_template_names_each_severity(severity: str) -> None:
    findings = "\n".join(lines("review.md"))
    assert f"[{severity}]" in findings


def test_review_template_has_a_single_verdict_line() -> None:
    verdicts = [line for line in lines("review.md") if line.startswith("Verdict:")]
    assert verdicts == ["Verdict: <approve | request changes>"]


def test_plan_acceptance_criteria_are_numbered() -> None:
    text = lines("plan.md")
    start = text.index("## Acceptance criteria")
    end = text.index("## Test cases")
    body = text[start + 1 : end]
    assert any(line.startswith("1. ") for line in body)
    assert not any(line.startswith("- ") for line in body)
