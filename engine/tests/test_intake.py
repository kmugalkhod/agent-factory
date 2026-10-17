"""Tests for factory_engine.intake (task 2.21): starting a run from a feature description or a
spec file, against a temporary repo with a bare "remote"."""

import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from factory_engine import events as ev
from factory_engine.config import default_settings
from factory_engine.errors import DataFolderError, GitError, IntakeError
from factory_engine.events import read_events
from factory_engine.git_ops import Git, run_branch, worktree_dir
from factory_engine.intake import INTENT_FILE, Intake, make_slug, request_of
from factory_engine.paths import repo_runs_dir, run_dir
from factory_engine.registry import Registry, registry_path
from factory_engine.run import load_run

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
REPO = "pilot"

# A spec with Windows line endings, bytes that aren't UTF-8, a NUL, its own `## ` headings
# (one of them `## Answers`), trailing spaces and no newline at the end.
SPEC = (
    b"# CSV export\r\n\r\n## Goal\r\n\r\nExport rows as CSV.  \r\n"
    b"Caf\xe9 names stay as they are (Latin-1 byte), \x00 and \xff\xfe too.\r\n"
    b"## Answers\r\nThis heading is part of the spec.\r\n\r\nLast line, no newline"
)


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


@dataclass
class Place:
    repo: Path  # the main checkout, cloned from the bare remote
    data: Path
    env: dict[str, str]
    registry: Registry

    def intake(self, repo_path: Path | None = None) -> Intake:
        return Intake(
            data=self.data,
            repo=REPO,
            repo_path=repo_path or self.repo,
            settings=default_settings(sys.platform),
            git=Git(self.env),
            registry=self.registry,
            clock=lambda: NOW,
        )


@pytest.fixture
def place(tmp_path: Path, git_env: dict[str, str]) -> Iterator[Place]:
    remote = tmp_path / "remote.git"
    sh(tmp_path, "init", "--bare", "-b", "main", str(remote), env=git_env)
    seed = tmp_path / "seed"
    sh(tmp_path, "init", "-b", "main", str(seed), env=git_env)
    sh(seed, "remote", "add", "origin", str(remote), env=git_env)
    (seed / "README.md").write_text("pilot\n", encoding="utf-8")
    sh(seed, "add", "-A", env=git_env)
    sh(seed, "commit", "-m", "first", env=git_env)
    sh(seed, "push", "origin", "main", env=git_env)
    repo = tmp_path / "repo"
    sh(tmp_path, "clone", str(remote), str(repo), env=git_env)
    data = tmp_path / "data"
    data.mkdir()
    registry = Registry(registry_path(data))
    yield Place(repo=repo, data=data, env=git_env, registry=registry)
    registry.close()


def write_spec(tmp_path: Path, data: bytes = SPEC, name: str = "CSV Export.md") -> Path:
    path = tmp_path / "specs" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    return path


def lines(data: bytes) -> list[bytes]:
    return data.split(b"\n")


# ------------------------------------------------------------------ a spec


def test_a_specs_bytes_are_preserved_in_intent_md(tmp_path: Path, place: Place) -> None:
    started = place.intake().spec(write_spec(tmp_path), requested_by="Kunal")

    intent = (started.run_folder / INTENT_FILE).read_bytes()
    assert SPEC in intent  # byte for byte: CRLF, Latin-1, NUL, no newline added inside
    assert request_of(intent) == SPEC
    head = b"# Intent: run-0-csv-export\n\nKind: spec\nRepo: pilot\n"
    assert intent.startswith(head)
    assert b"Requested by: Kunal, 2026-10-17\n" in intent
    assert intent.endswith(SPEC + b"\n\n## Answers\n")


@pytest.mark.parametrize(
    "spec",
    [
        b"One line\n",
        b"\xef\xbb\xbfA spec with a BOM\r\n",
        b"Ends with a lone CR\r",
        b"x",
        b"## Request\n## Answers\n",
    ],
    ids=["lf", "bom-crlf", "cr", "one-byte", "template-headings"],
)
def test_any_spec_comes_back_exactly(tmp_path: Path, place: Place, spec: bytes) -> None:
    started = place.intake().spec(write_spec(tmp_path, spec), requested_by="Kunal")
    assert request_of((started.run_folder / INTENT_FILE).read_bytes()) == spec


def test_a_spec_run_is_created_registered_and_given_its_worktree(
    tmp_path: Path, place: Place
) -> None:
    spec = write_spec(tmp_path)
    started = place.intake().spec(spec, requested_by="Kunal")

    run = started.run
    assert (run.id, run.slug, run.repo, run.kind, run.state) == (
        0,
        "csv-export",
        REPO,
        "spec",
        "pending",
    )
    assert (run.provider, run.created_at) == ("subscription", NOW)
    assert started.run_folder == run_dir(place.data, REPO, 0, "csv-export")
    assert load_run(started.run_folder) == run
    assert place.registry.get_run(REPO, 0) == run
    assert started.worktree == worktree_dir(place.data, REPO, 0, "csv-export")
    branch = sh(started.worktree, "branch", "--show-current", env=place.env)
    assert branch == run_branch(0, "csv-export")
    assert (started.worktree / "README.md").read_text(encoding="utf-8") == "pilot\n"
    [created] = read_events(started.run_folder)
    assert created.body == ev.RunCreated(kind="spec", spec=str(spec.resolve()))
    assert (started.events.repo, started.events.run_id) == (REPO, 0)


def test_the_main_checkout_is_left_alone(tmp_path: Path, place: Place) -> None:
    before = sh(place.repo, "status", "--porcelain", "--branch", env=place.env)
    place.intake().spec(write_spec(tmp_path), requested_by="Kunal")
    assert sh(place.repo, "status", "--porcelain", "--branch", env=place.env) == before


@pytest.mark.parametrize("make", ["missing", "folder", "empty", "blank"])
def test_a_spec_that_cant_be_used_is_refused_before_a_run_exists(
    tmp_path: Path, place: Place, make: str
) -> None:
    path = tmp_path / "spec.md"
    if make == "folder":
        path.mkdir()
    elif make == "empty":
        path.write_bytes(b"")
    elif make == "blank":
        path.write_bytes(b" \r\n\t\n")

    with pytest.raises(IntakeError, match="spec"):
        place.intake().spec(path, requested_by="Kunal")
    assert not repo_runs_dir(place.data, REPO).exists()


# ------------------------------------------------------------------ a feature description


def test_a_description_is_written_in_the_intent_templates_shape(place: Place) -> None:
    text = "Add `mul(a, b)` to `src/calc.py`.\nKeep `add` as it is."
    started = place.intake().feature(text, requested_by="Kunal")

    intent = (started.run_folder / INTENT_FILE).read_bytes().decode("utf-8")
    assert intent == (
        "# Intent: run-0-add-mul-a-b-to-src-calc-py\n"
        "\n"
        "Kind: feature\n"
        "Repo: pilot\n"
        "Requested by: Kunal, 2026-10-17\n"
        "\n"
        "## Request\n"
        "\n"
        f"{text}\n"
        "\n"
        "## Answers\n"
    )
    assert started.run.kind == "feature"
    assert [e.body for e in read_events(started.run_folder)] == [ev.RunCreated(kind="feature")]


def test_run_ids_go_up_per_repo(place: Place) -> None:
    intake = place.intake()
    first = intake.feature("Add mul", requested_by="Kunal")
    second = intake.feature("Add div", requested_by="Kunal")
    assert (first.run.id, second.run.id) == (0, 1)
    assert [run.id for run in place.registry.list_runs(REPO)] == [0, 1]


def test_a_given_slug_is_used(place: Place) -> None:
    started = place.intake().feature("Add mul", requested_by="Kunal", slug="mul")
    assert started.run.slug == "mul"
    assert started.run_folder.name == "0-mul"


def test_a_bad_slug_is_refused(place: Place) -> None:
    with pytest.raises(DataFolderError, match="slug"):
        place.intake().feature("Add mul", requested_by="Kunal", slug="Not A Slug")
    assert not repo_runs_dir(place.data, REPO).exists()


@pytest.mark.parametrize(
    ("description", "requested_by"),
    [("", "Kunal"), ("  \n\t", "Kunal"), ("Add mul", ""), ("Add mul", "Kunal\nKind: spec")],
    ids=["empty", "blank", "no-name", "name-with-newline"],
)
def test_an_unusable_request_is_refused(place: Place, description: str, requested_by: str) -> None:
    with pytest.raises(IntakeError):
        place.intake().feature(description, requested_by=requested_by)
    assert not repo_runs_dir(place.data, REPO).exists()


@pytest.mark.parametrize(
    ("text", "slug"),
    [
        ("Add `mul(a, b)` to calc", "add-mul-a-b-to-calc"),
        ("First line is the title\nnot this one", "first-line-is-the-title"),
        ("\n\n  Blank lines first\nthen more", "blank-lines-first"),
        ("CSV Export v2", "csv-export-v2"),
        ("  --Hello__World--  ", "hello-world"),
        (
            "one two three four five six seven eight nine ten",
            "one-two-three-four-five-six-seven-eight",
        ),
        ("a" * 50, "a" * 40),
        ("日本語", "request"),
        ("", "request"),
    ],
)
def test_slugs_are_made_from_the_request(text: str, slug: str) -> None:
    assert make_slug(text, fallback="request") == slug


# ------------------------------------------------------------------ the worktree


def test_a_worktree_that_cant_be_made_fails_the_run(place: Place, tmp_path: Path) -> None:
    lonely = tmp_path / "lonely"  # a repo with no `origin` to fetch main from
    sh(tmp_path, "init", "-b", "main", str(lonely), env=place.env)

    with pytest.raises(GitError, match="worktree"):
        place.intake(repo_path=lonely).feature("Add mul", requested_by="Kunal")

    folder = run_dir(place.data, REPO, 0, "add-mul")
    run = load_run(folder)
    assert run.state == "failed"
    assert place.registry.get_run(REPO, 0) == run
    assert [type(e.body) for e in read_events(folder)] == [ev.RunCreated, ev.StateChanged]
    assert (folder / INTENT_FILE).is_file()  # the request is kept for a new start
    assert "Status: failed" in (folder / "report.md").read_text(encoding="utf-8")  # 2.27


def test_request_of_refuses_a_file_without_the_headings() -> None:
    with pytest.raises(IntakeError):
        request_of(b"# Intent\n\nNo headings here.\n")


def test_on_claim_runs_once_the_folder_is_claimed_before_the_run_is_written(place: Place) -> None:
    seen: list[tuple[Path, list[str]]] = []

    def claim(folder: Path) -> None:
        seen.append((folder, sorted(path.name for path in folder.iterdir())))

    started = replace(place.intake(), on_claim=claim).feature("Add mul", requested_by="Kunal")

    assert seen == [(started.run_folder, [])]


def test_a_refusing_on_claim_publishes_nothing(place: Place) -> None:
    def refuse(folder: Path) -> None:
        raise IntakeError("someone else holds the run")

    with pytest.raises(IntakeError, match="someone else"):
        replace(place.intake(), on_claim=refuse).feature("Add mul", requested_by="Kunal")

    folder = run_dir(place.data, REPO, 0, "add-mul")
    assert sorted(path.name for path in folder.iterdir()) == []
    assert place.registry.get_run(REPO, 0) is None
