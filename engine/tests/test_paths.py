"""Tests for factory_engine.paths (task 2.2): the data folder and per-run folders."""

from pathlib import Path

import pytest

from factory_engine.errors import DataFolderError
from factory_engine.paths import DATA_ENV, data_dir, repo_runs_dir, run_dir


def test_data_dir_defaults_to_localappdata(tmp_path: Path) -> None:
    assert data_dir({"LOCALAPPDATA": str(tmp_path)}) == tmp_path / "agent-factory"


def test_data_dir_env_override_wins(tmp_path: Path) -> None:
    env = {"LOCALAPPDATA": str(tmp_path / "local"), DATA_ENV: str(tmp_path / "custom")}
    assert data_dir(env) == tmp_path / "custom"


def test_empty_override_falls_back_to_default(tmp_path: Path) -> None:
    env = {"LOCALAPPDATA": str(tmp_path), DATA_ENV: "  "}
    assert data_dir(env) == tmp_path / "agent-factory"


def test_data_dir_without_any_location_names_the_env_var() -> None:
    with pytest.raises(DataFolderError) as exc:
        data_dir({})
    assert DATA_ENV in str(exc.value)


def test_run_dir_is_data_runs_repo_id_slug(tmp_path: Path) -> None:  # AC3
    assert run_dir(tmp_path, "agent-factory", 7, "status-json") == (
        tmp_path / "runs" / "agent-factory" / "7-status-json"
    )
    assert repo_runs_dir(tmp_path, "agent-factory") == tmp_path / "runs" / "agent-factory"


@pytest.mark.parametrize("repo", ["", ".", "..", "a/b", "a\\b", "C:", "with space", "x" * 101])
def test_unsafe_repo_name_is_refused(tmp_path: Path, repo: str) -> None:
    with pytest.raises(DataFolderError) as exc:
        repo_runs_dir(tmp_path, repo)
    assert "repo" in str(exc.value)


@pytest.mark.parametrize(
    "repo", ["CON", "con", "Nul", "PRN", "aux", "COM1", "lpt9", "CON.txt", "nul.git", "app."]
)
def test_windows_reserved_or_trimmed_repo_name_is_refused(tmp_path: Path, repo: str) -> None:
    """PR #12 review: device names open a device, and Windows drops a trailing period."""
    with pytest.raises(DataFolderError) as exc:
        repo_runs_dir(tmp_path, repo)
    assert repo in str(exc.value)


@pytest.mark.parametrize("repo", ["console", "com10", "lpt", "my.app", "connect"])
def test_names_that_only_look_reserved_are_allowed(tmp_path: Path, repo: str) -> None:
    assert repo_runs_dir(tmp_path, repo) == tmp_path / "runs" / repo


@pytest.mark.parametrize("slug", ["", "Add-CSV", "a/b", "a b", "-lead", "trail-", "x" * 41])
def test_invalid_slug_is_refused(tmp_path: Path, slug: str) -> None:
    with pytest.raises(DataFolderError) as exc:
        run_dir(tmp_path, "repo", 1, slug)
    assert "slug" in str(exc.value)


def test_negative_run_id_is_refused(tmp_path: Path) -> None:
    with pytest.raises(DataFolderError):
        run_dir(tmp_path, "repo", -1, "x")
