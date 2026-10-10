"""The factory data folder and the folders inside it. Names are checked before they become paths."""

import re
from collections.abc import Mapping
from pathlib import Path

from factory_engine.errors import DataFolderError

DATA_ENV = "AGENT_FACTORY_DATA"
"""Environment variable that overrides the data folder."""

REPO_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")
SLUG = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
MAX_SLUG = 40


def data_dir(env: Mapping[str, str]) -> Path:
    """`$AGENT_FACTORY_DATA` if set, else `%LOCALAPPDATA%\\agent-factory`. Pass `os.environ`."""
    override = env.get(DATA_ENV, "").strip()
    if override:
        return Path(override)
    local = env.get("LOCALAPPDATA", "").strip()
    if local:
        return Path(local) / "agent-factory"
    raise DataFolderError(
        f"Can't locate the factory data folder: neither {DATA_ENV} nor LOCALAPPDATA is set. "
        f"Set {DATA_ENV} to the folder to use."
    )


def repo_runs_dir(data: Path, repo: str) -> Path:
    """`<data>/runs/<repo>/`: every run of one repo. Run IDs count up per repo."""
    if not REPO_NAME.fullmatch(repo) or repo in (".", ".."):
        raise DataFolderError(
            f"repo name {repo!r} can't be used as a folder name. "
            "Use 1 to 100 letters, digits, '.', '_' or '-'."
        )
    return data / "runs" / repo


def run_dir(data: Path, repo: str, run_id: int, slug: str) -> Path:
    """`<data>/runs/<repo>/<id>-<slug>/`: one run's docs, `run.json` and logs."""
    if run_id < 0:
        raise DataFolderError(f"run id {run_id} is negative. Run ids start at 0.")
    check_slug(slug)
    return repo_runs_dir(data, repo) / f"{run_id}-{slug}"


def check_slug(slug: str) -> None:
    if len(slug) > MAX_SLUG or not SLUG.fullmatch(slug):
        raise DataFolderError(
            f"slug {slug!r} is invalid. Use at most {MAX_SLUG} lowercase letters and digits, "
            "in words joined by single '-'."
        )
