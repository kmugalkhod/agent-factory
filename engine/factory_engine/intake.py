"""Intake: start a run from a feature description or a spec file (design: how the start
differs; templates: intent.md).

A start allocates the run (its ID, slug and folder), writes `intent.md`, saves `run.json`,
indexes the run, logs `run_created`, and creates the run's branch and worktree from the latest
`main`. The run is left `pending`; `Pipeline.advance` takes it from there.

`intent.md` keeps the intent template's shape for both kinds of request: a short header, then
`## Request` with the request, then `## Answers` as the last heading (answers are appended under
it, see `decisions.py`). The request goes in byte for byte: a spec file's bytes are copied
unchanged (line endings, encoding and all; nothing is decoded), and a description is written as
UTF-8. The frame around it is fixed, so `request_of` gives the request back exactly.
"""

import contextlib
import logging
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from factory_engine.agents.runner import RunIndex, persist
from factory_engine.config import Settings
from factory_engine.errors import FactoryError, GitError, IntakeError
from factory_engine.events import EventLog, EventObserver, GateResult, RunCreated
from factory_engine.git_ops import Git, create_worktree
from factory_engine.paths import MAX_SLUG, check_slug
from factory_engine.providers import get_profile
from factory_engine.report import REPORT_GATE, write_report
from factory_engine.run import Kind, Run, allocate_run, new_run
from factory_engine.states import move

logger = logging.getLogger(__name__)

INTENT_FILE = "intent.md"
REQUEST_HEADING = b"## Request"
ANSWERS_HEADING = b"## Answers"
FALLBACK_SLUG = {"feature": "feature", "spec": "spec", "bootstrap": "bootstrap"}

# What follows the request: a blank line, then the Answers heading. Always the same bytes, so
# the request can be cut back out exactly, whatever it ends with.
_AFTER_REQUEST = b"\n\n" + ANSWERS_HEADING + b"\n"
_NOT_SLUG = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class Started:
    """A run intake created: `pending`, with its folder, worktree and event log (pass the log
    to the pipeline; one writer per run)."""

    run: Run
    run_folder: Path
    worktree: Path
    events: EventLog


@dataclass(frozen=True)
class Intake:
    """Starts runs on one repo. Everything it uses is passed in: the data folder, the repo's
    name and main checkout, the run's effective settings (provider and isolation), git, the
    registry and a clock, and optionally what to do once a run's folder is claimed and who watches
    the run's events."""

    data: Path
    repo: str
    repo_path: Path
    settings: Settings
    git: Git
    registry: RunIndex
    clock: Callable[[], datetime]
    remote: str = "origin"
    base: str = "main"
    on_claim: Callable[[Path], None] | None = None
    """Called with the run folder as soon as it is claimed, before anything is written into it
    or indexed (the CLI takes the run's lock there). If it raises, the run isn't published."""
    observer: EventObserver | None = None
    """Given to the run's event log: told about each event once it is written (`EventLog`)."""

    def feature(self, description: str, *, requested_by: str, slug: str | None = None) -> Started:
        """Start a feature run from your description, written into `intent.md` as given. The
        slug comes from its first line unless you give one."""
        if not description.strip():
            raise IntakeError("The feature description is empty. Say what the feature should do.")
        return self._start(
            "feature",
            description.encode("utf-8"),
            requested_by=requested_by,
            slug=slug or make_slug(description, fallback=FALLBACK_SLUG["feature"]),
            spec=None,
        )

    def spec(self, path: Path, *, requested_by: str, slug: str | None = None) -> Started:
        """Start a spec run: the file's bytes go into `intent.md` unchanged. The slug comes from
        the file name unless you give one."""
        try:
            data = path.read_bytes()
        except OSError as err:
            raise IntakeError(
                f"Can't read the spec {path} ({err}). Give the path of the spec file."
            ) from err
        if not data.strip():
            raise IntakeError(f"The spec {path} is empty. Write the spec, then start the run.")
        return self._start(
            "spec",
            data,
            requested_by=requested_by,
            slug=slug or make_slug(path.stem, fallback=FALLBACK_SLUG["spec"]),
            spec=str(path.resolve()),
        )

    def _start(
        self, kind: Kind, request: bytes, *, requested_by: str, slug: str, spec: str | None
    ) -> Started:
        who = requested_by.strip()
        if not who or "\n" in who or "\r" in who:
            raise IntakeError(
                f"requested_by {requested_by!r} must be a name on one line. Give who asked for "
                "the run."
            )
        check_slug(slug)
        now = self.clock()
        run_id, folder = allocate_run(self.data, self.repo, slug)
        if self.on_claim is not None:
            self.on_claim(folder)
        intent = folder / INTENT_FILE
        header = (
            f"# Intent: run-{run_id}-{slug}\n\nKind: {kind}\nRepo: {self.repo}\n"
            f"Requested by: {who}, {now.date().isoformat()}\n\n"
        ).encode()
        try:
            write_bytes_atomic(intent, header + frame_request(request))
        except OSError as err:
            raise IntakeError(
                f"{intent}: couldn't write ({err}). Run ID {run_id} stays claimed; check the "
                "data folder, then start the run again."
            ) from err
        run = new_run(
            run_id=run_id,
            slug=slug,
            repo=self.repo,
            kind=kind,
            provider=self.settings.provider,
            isolation=self.settings.isolation,
            now=now,
        )
        persist(run, folder, self.registry)
        events = EventLog(
            folder, repo=self.repo, run_id=run_id, clock=self.clock, observer=self.observer
        )
        events.append(RunCreated(kind=kind, spec=spec))
        try:
            worktree = create_worktree(
                self.git,
                self.repo_path,
                self.data,
                repo_name=self.repo,
                run_id=run_id,
                slug=slug,
                remote=self.remote,
                base=self.base,
            )
        except GitError as err:
            failed = move(run, "failed", log=events)
            persist(failed, folder, self.registry)
            _report_failure(failed, folder, self.settings, events)
            raise GitError(
                f"run {self.repo}/{run_id} failed: its worktree couldn't be created. {err} "
                f"Fix that, then start the run again; the request is kept in {intent}."
            ) from err
        return Started(run=run, run_folder=folder, worktree=worktree, events=events)


def _report_failure(run: Run, folder: Path, settings: Settings, events: EventLog) -> None:
    """The failed run's `report.md`. A report that can't be written is logged in the run's
    events (`gate_result` `report`), and the intake failure is raised as it was."""
    try:
        profile = get_profile(run.provider)
    except FactoryError:
        profile = None
    try:
        write_report(run, folder, settings=settings, profile=profile)
    except FactoryError as err:
        try:
            events.append(GateResult(gate=REPORT_GATE, passed=False, reason=str(err)))
        except FactoryError:
            logger.warning("run %s/%s: report.md couldn't be written: %s", run.repo, run.id, err)


def frame_request(request: bytes) -> bytes:
    """`## Request`, the request's bytes unchanged, then `## Answers` (the last heading)."""
    return REQUEST_HEADING + b"\n\n" + request + _AFTER_REQUEST


def request_of(intent: bytes) -> bytes:
    """The request in an `intent.md` intake wrote, exactly as it was given: everything between
    the first `## Request` line and the last `## Answers` line, less the fixed frame."""
    lines = intent.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    heads = [line.rstrip(b"\r\n") for line in lines]
    try:
        first = heads.index(REQUEST_HEADING)
        last = len(heads) - 1 - heads[::-1].index(ANSWERS_HEADING)
    except ValueError as err:
        raise IntakeError(
            f"intent.md has no `{REQUEST_HEADING.decode()}` or `{ANSWERS_HEADING.decode()}` "
            "line, so it wasn't written by intake. Keep both headings."
        ) from err
    start = offsets[first] + len(REQUEST_HEADING) + 2  # the heading line and one blank line
    end = offsets[last] - 2  # the blank line before `## Answers`
    if end < start or intent[start - 2 : start] != b"\n\n" or intent[end : end + 2] != b"\n\n":
        raise IntakeError("intent.md's Request section isn't framed as intake writes it.")
    return intent[start:end]


def make_slug(text: str, *, fallback: str) -> str:
    """The lowercase words of `text`'s first non-blank line joined by `-`, whole words up to
    the slug limit; `fallback` when no letter or digit is left (a title in another script)."""
    first = next((line for line in text.splitlines() if line.strip()), "")
    words = [word for word in _NOT_SLUG.split(first.lower()) if word]
    slug = ""
    for word in words:
        joined = f"{slug}-{word}" if slug else word
        if len(joined) > MAX_SLUG:
            break
        slug = joined
    if not slug and words:
        slug = words[0][:MAX_SLUG]
    return slug or fallback


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Write `path` through a temp file in the same folder and a replace, so a crash leaves
    the old file or the new one. Raises OSError."""
    fd, name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)
        raise
