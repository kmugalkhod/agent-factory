"""Factory-wide skills: valid frontmatter, short bodies, and template copies kept in sync."""

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SKILLS = REPO / "plugin" / "skills"
TEMPLATES = REPO / "templates"
MAX_BODY_LINES = 150

# Each skill bundles a copy of the template it fills; the copy must match the source exactly.
BUNDLED = {
    "write-plan": "plan.md",
    "write-handoff": "handoff.md",
    "review-rubric": "review.md",
    "acceptance-criteria": "plan.md",
}


def skill_folders() -> list[Path]:
    return sorted(p for p in SKILLS.iterdir() if p.is_dir()) if SKILLS.is_dir() else []


def read(path: Path) -> str:
    """File text with line endings normalized; git on Windows may check out CRLF."""
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def split(folder: Path) -> tuple[dict[str, object], list[str]]:
    """Return a SKILL.md's parsed frontmatter and its body lines."""
    lines = read(folder / "SKILL.md").split("\n")
    assert lines[0] == "---", "SKILL.md must start with a `---` frontmatter line"
    end = lines.index("---", 1)
    meta = yaml.safe_load("\n".join(lines[1:end]))
    assert isinstance(meta, dict), "frontmatter must be a YAML mapping"
    body = lines[end + 1 :]
    if body and body[-1] == "":
        body = body[:-1]  # the final newline doesn't start another line
    return meta, body


def test_the_four_factory_skills_exist() -> None:
    names = {p.name for p in skill_folders()}
    assert set(BUNDLED) <= names


def test_every_skill_folder_has_a_skill_md() -> None:
    for folder in skill_folders():
        assert (folder / "SKILL.md").is_file(), folder.name


@pytest.mark.parametrize("folder", skill_folders(), ids=lambda p: p.name)
def test_frontmatter_name_matches_the_folder(folder: Path) -> None:
    meta, _ = split(folder)
    assert meta.get("name") == folder.name


@pytest.mark.parametrize("folder", skill_folders(), ids=lambda p: p.name)
def test_description_says_when_to_use_the_skill(folder: Path) -> None:
    meta, _ = split(folder)
    description = meta.get("description")
    assert isinstance(description, str)
    assert re.search(r"\bUse (it )?when\b", description), description


@pytest.mark.parametrize("folder", skill_folders(), ids=lambda p: p.name)
def test_body_is_under_the_line_limit(folder: Path) -> None:
    _, body = split(folder)
    assert len(body) < MAX_BODY_LINES


@pytest.mark.parametrize("folder", skill_folders(), ids=lambda p: p.name)
def test_skill_never_mentions_git(folder: Path) -> None:
    """Agents never run git; the engine commits. A skill must not suggest otherwise."""
    assert not re.search(r"\bgit\b", read(folder / "SKILL.md"), re.IGNORECASE)


@pytest.mark.parametrize(("skill", "template"), BUNDLED.items())
def test_bundled_template_equals_its_source(skill: str, template: str) -> None:
    copy = SKILLS / skill / template
    assert copy.is_file(), f"{skill} must bundle a copy of templates/{template}"
    assert read(copy) == read(TEMPLATES / template)


@pytest.mark.parametrize(("skill", "template"), BUNDLED.items())
def test_skill_names_its_bundled_template(skill: str, template: str) -> None:
    _, body = split(SKILLS / skill)
    assert f"`{template}`" in "\n".join(body)
