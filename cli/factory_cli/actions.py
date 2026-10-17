"""The engine actions the `factory` command calls, and what they give back.

`EngineActions` is the boundary between the command (`app.py`) and the engine: the real one
(`engine.py`) wires the engine's objects for one repo; tests pass a stub. The run-driving
actions (start, approve and its variants, resume) run the pipeline in the foreground and return
where the run stopped.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, Protocol

from factory_engine.pipeline import Stop
from factory_engine.run import Role, Run
from factory_engine.usage import Grouping, Usage

type Ending = Stop | Literal["interrupted"]
"""Where the pipeline stopped (`PipelineResult.stop`), or `interrupted` by Ctrl-C."""


@dataclass(frozen=True)
class Outcome:
    """A run after the command drove it: where it stopped, its folder, the questions its plan
    asks (when it waits for answers) and why it needs you or is paused."""

    run: Run
    stop: Ending
    run_folder: Path
    questions: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class RunRow:
    """One run in `factory status`. `running`: a factory process is driving it now."""

    run: Run
    running: bool


@dataclass(frozen=True)
class RunDetail:
    """One run in `factory status <run>`."""

    run: Run
    running: bool
    run_folder: Path
    worktree: Path
    questions: tuple[str, ...]
    reason: str | None


class EngineActions(Protocol):
    """What the `factory` command asks the engine to do, on the repo it was given."""

    def start_feature(self, description: str, *, slug: str | None) -> Outcome: ...

    def start_spec(self, path: Path, *, slug: str | None) -> Outcome: ...

    def runs(self, *, all_repos: bool) -> list[RunRow]: ...

    def describe(self, run_id: int) -> RunDetail: ...

    def approve(self, run_id: int) -> Outcome: ...

    def edit_and_approve(self, run_id: int, plan_file: Path) -> Outcome: ...

    def send_back(self, run_id: int, notes: str) -> Outcome: ...

    def answer(self, run_id: int, answers: Sequence[str]) -> Outcome: ...

    def stop(self, run_id: int) -> Run: ...

    def resume(self, run_id: int, *, note: str | None) -> Outcome: ...

    def attach(self, run_id: int, *, role: Role | None) -> str: ...

    def usage(self, *, by: Grouping, since: date | None, all_repos: bool) -> Usage:
        """Token totals by `by` from the UTC day `since` on, for the repo or every repo."""
        ...
