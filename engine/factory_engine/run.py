"""The `Run` model, its `run.json` file and run ID allocation.

`run.json` is the run's source of truth: the engine continues from `last_completed_step` after a
crash. It is written atomically, so a crash mid-write leaves the previous version intact.
"""

import contextlib
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from factory_engine.errors import DataFolderError, RunFileError
from factory_engine.paths import repo_runs_dir, run_dir

Role = Literal["planner", "tester", "builder", "reviewer"]
Kind = Literal["bootstrap", "feature", "spec"]
Isolation = Literal["worktree", "sandbox"]
RunState = Literal[
    "pending",
    "planning",
    "awaiting_plan_approval",
    "testing",
    "building",
    "reviewing",
    "updating",
    "awaiting_merge",
    "merged",
    "needs_you",
    "paused",
    "failed",
    "stopped",
    "closed",
]
NonNegative = Annotated[int, Field(ge=0)]

RUN_FILE = "run.json"
CLAIMS = ".ids"
CLAIMED_ID = re.compile(r"\d+")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Metrics(_Model):
    """Per-run quality data (design: "Quality metrics"). Reporting arrives with the UI."""

    tokens: dict[Role, NonNegative] = {}
    important_findings: NonNegative = 0
    pr_lines_changed_by_you: NonNegative | None = None


class Run(_Model):
    id: NonNegative
    slug: str
    repo: str
    kind: Kind
    state: RunState = "pending"
    stage: Role | None = None
    attempts: dict[Role, NonNegative] = {}
    session_ids: dict[Role, str] = {}
    provider: str
    isolation: Isolation
    created_at: AwareDatetime
    updated_at: AwareDatetime
    metrics: Metrics = Metrics()
    last_completed_step: str | None = None


def new_run(
    *,
    run_id: int,
    slug: str,
    repo: str,
    kind: Kind,
    provider: str,
    isolation: Isolation,
    now: datetime,
) -> Run:
    """A pending run. `now` must be timezone-aware; pass it in so tests control the clock."""
    return Run(
        id=run_id,
        slug=slug,
        repo=repo,
        kind=kind,
        provider=provider,
        isolation=isolation,
        created_at=now,
        updated_at=now,
    )


def save_run(run: Run, folder: Path) -> None:
    """Write `folder/run.json` atomically: a temp file in the same folder, then a replace."""
    target = folder / RUN_FILE
    temp: Path | None = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix=f"{RUN_FILE}.", suffix=".tmp", dir=folder)
        temp = Path(temp_name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(run.model_dump_json(indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(target)  # atomic on the same volume (os.replace)
    except OSError as err:
        if temp is not None:
            with contextlib.suppress(OSError):  # a failed cleanup must not hide `err`
                temp.unlink(missing_ok=True)
        raise RunFileError(
            f"{target}: couldn't write ({err}). The previous version is unchanged; retry the save."
        ) from err


def load_run(folder: Path) -> Run:
    target = folder / RUN_FILE
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise RunFileError(f"{target}: can't read ({err}). Check the run folder.") from err
    try:
        return Run.model_validate_json(text)
    except ValidationError as err:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in item['loc']) or '<root>'}: {item['msg']}"
            for item in err.errors()
        )
        raise RunFileError(f"{target}: invalid run file ({problems}).") from err


def allocate_run(data: Path, repo: str, slug: str) -> tuple[int, Path]:
    """Claim the repo's next run ID and create its folder. Returns the ID and the folder.

    IDs start at 0 (the bootstrap run) and only go up. Each ID is claimed by creating an empty
    file `<data>/runs/<repo>/.ids/<id>` exclusively, so concurrent callers never get the same
    ID, and an ID stays used even if its run folder is later removed.
    """
    claims = repo_runs_dir(data, repo) / CLAIMS
    run_dir(data, repo, 0, slug)  # validate repo and slug before touching the disk
    try:
        claims.mkdir(parents=True, exist_ok=True)
        run_id = 1 + max(
            (int(p.name) for p in claims.iterdir() if CLAIMED_ID.fullmatch(p.name)), default=-1
        )
    except OSError as err:
        raise DataFolderError(
            f"{claims}: can't create or read the run ID claims ({err}). "
            "Check the data folder exists as a writable folder."
        ) from err
    while True:
        claim = claims / str(run_id)
        try:
            claim.touch(exist_ok=False)
            break
        except FileExistsError:
            run_id += 1
        except OSError as err:
            raise DataFolderError(
                f"{claim}: can't claim run ID {run_id} ({err}). Check {claims} is writable."
            ) from err
    folder = run_dir(data, repo, run_id, slug)
    try:
        folder.mkdir()
    except OSError as err:
        raise DataFolderError(
            f"{folder}: can't create the run folder ({err}). Run ID {run_id} stays claimed; "
            "clear that path and start the run again."
        ) from err
    return run_id, folder
