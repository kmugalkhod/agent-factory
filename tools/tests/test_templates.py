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


def test_review_sections_match_the_acceptance_criteria() -> None:
    assert contract()["review.md"] == [
        ("Acceptance criteria", False),
        ("Findings", False),
        ("Verdict", False),
    ]


def finding_format() -> str:
    """The finding line format from README.md, with the severity left as a placeholder."""
    prefix = "- Finding: ``"
    (line,) = [x for x in README.read_text(encoding="utf-8").splitlines() if x.startswith(prefix)]
    return line.removeprefix(prefix).removesuffix("``")


def test_readme_finding_format_names_every_severity_and_a_fix() -> None:
    fmt = finding_format()
    assert fmt.startswith("- [<Important | Minor | Nit>] ")
    assert ". Fix: " in fmt


@pytest.mark.parametrize("severity", ["Important", "Minor", "Nit"])
def test_review_example_finding_matches_the_readme_format(severity: str) -> None:
    expected = finding_format().replace("<Important | Minor | Nit>", severity)
    assert expected in lines("review.md")


def test_review_has_no_example_finding_outside_the_readme_format() -> None:
    examples = [line for line in lines("review.md") if line.startswith("- [")]
    fmt = finding_format()
    for line in examples:
        severity = line[3 : line.index("]")]
        assert line == fmt.replace("<Important | Minor | Nit>", severity)


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


def test_readme_says_earlier_reviews_are_archived() -> None:
    """Run 1 (task 1.5): a second review overwrote the first with no rule for keeping it."""
    assert "`attempts/review-<n>.md`" in README.read_text(encoding="utf-8")
