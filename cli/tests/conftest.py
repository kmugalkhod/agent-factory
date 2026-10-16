"""Shared fixtures for the CLI tests."""

import os
from pathlib import Path

import pytest


@pytest.fixture
def git_env(tmp_path: Path) -> dict[str, str]:
    """The test's environment with no `GIT_*` leftovers, an empty global git config and a fixed
    identity, so the user's own git settings (signing, hooks, aliases) can't change a test."""
    config = tmp_path / "gitconfig"
    config.write_text("", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=str(config),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Factory Test",
        GIT_AUTHOR_EMAIL="factory@example.com",
        GIT_COMMITTER_NAME="Factory Test",
        GIT_COMMITTER_EMAIL="factory@example.com",
    )
    return env
