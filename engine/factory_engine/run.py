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
PauseReason = Literal["usage_limit", "turn_cap", "time_cap"]
NonNegative = Annotated[int, Field(ge=0)]

RUN_FILE = "run.json"
ATTEMPT_FILE = "attempt.json"
"""The attempt in flight and what the next one is told, for crash recovery (`recovery.py`)."""
CLAIMS = ".ids"
CLAIMED_ID = re.compile(r"\d+")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TokenUse(_Model):
    """The tokens of one agent turn (one result message): whose, which attempt, and when, so
    usage can be totalled by run, role, attempt and day."""

    role: Role
    attempt: Annotated[int, Field(ge=1)]
    at: AwareDatetime
    input: NonNegative
    output: NonNegative
    cache_read: NonNegative = 0
    cache_creation: NonNegative = 0

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_creation


class PauseRecord(_Model):
    """A pause the run went through (a usage limit, a turn or time cap), kept after the run
    carries on, so how often runs hit the usage limit can be counted."""

    reason: PauseReason
    role: Role
    attempt: Annotated[int, Field(ge=1)]
    at: AwareDatetime
    resume_at: AwareDatetime | None


class Metrics(_Model):
    """Per-run quality data (design: "Quality metrics"; `metrics.py` keeps it up to date).

    - `tokens`: the total per role; `token_use`: each turn's tokens, per attempt and dated.
    - `first_pass`: whether the first build passed its tests and the review; None until decided.
    - `builder_retries`: builder attempts after the first.
    - `important_findings`: summed over every review of the run.
    - `pauses`: every pause, usage limits included.
    - `pr_lines_changed_by_you`: lines added or removed in the run's change between the head the
      pull request opened at and the head merged, main's changes left out; None until the
      engine merges.
    """

    tokens: dict[Role, NonNegative] = {}
    token_use: list[TokenUse] = []
    first_pass: bool | None = None
    builder_retries: NonNegative = 0
    important_findings: NonNegative = 0
    pauses: list[PauseRecord] = []
    pr_lines_changed_by_you: NonNegative | None = None


class Pause(_Model):
    """Where a paused run stopped, so it can carry on in the same session (design: failure
    handling). A usage limit resumes on its own at `resume_at`; a cap waits for you."""

    reason: PauseReason
    detail: str
    state: RunState  # the working state the run resumes into
    role: Role
    attempt: Annotated[int, Field(ge=1)]
    session_id: str | None
    resume_at: AwareDatetime | None


class PullRequestInfo(_Model):
    """The run's pull request (git flow step 5). `head` is the commit last pushed, whose tests
    passed. `rebased_from` is the head you approved, when the engine rebased it onto a newer
    main at merge time (step 6); None otherwise. `opened_head` is the head it was opened at,
    for the lines you changed before the merge (2.27)."""

    number: Annotated[int, Field(ge=1)]
    url: str
    head: str
    rebased_from: str | None = None
    opened_head: str | None = None  # the head it was opened at: what you were first shown


class ApprovedMerge(_Model):
    """Your approve action: merge pull request `pr_number` as it was at `head`. Only
    `decisions.approve_merge` records one, and a merge uses it once."""

    pr_number: Annotated[int, Field(ge=1)]
    head: str


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
    pause: Pause | None = None  # set while the run is paused
    pull_request: PullRequestInfo | None = None  # once the pull request is open
    merge_approval: ApprovedMerge | None = None  # your approval, until the merge uses it


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
