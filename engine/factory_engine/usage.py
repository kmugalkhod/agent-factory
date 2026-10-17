"""Token totals for `factory usage` (task 2.29), grouped by run, repo, role or UTC day, with
how often runs hit the usage limit.

Read from the registry's `token_use` and `pauses` tables (task 2.27): one row per agent turn
and one per pause. Input, output, cache read and cache creation tokens are kept apart, since
cache reads are most of a run's tokens. A usage-limit hit is a pause with reason `usage_limit`
(turn and time caps don't count). Days are UTC days, as the registry stores them. No model
calls: plain sums over what the registry holds.
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from factory_engine.registry import PauseRow, Registry, TokenUseRow, UsageRecords
from factory_engine.run import Role

type Grouping = Literal["run", "repo", "role", "day"]
GROUPINGS: tuple[Grouping, ...] = ("run", "repo", "role", "day")
ROLE_ORDER: tuple[Role, ...] = ("planner", "tester", "builder", "reviewer")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UsageTotals(_Model):
    """Tokens and usage-limit hits over a set of agent turns and pauses.

    `turns`: agent turns (one `token_use` row each); `total`: the four token counts summed;
    `runs`: the runs with a turn or pause in the set; `usage_limit_hits`: usage-limit pauses;
    `runs_hit_usage_limit`: the runs with at least one.
    """

    turns: int
    input: int
    output: int
    cache_read: int
    cache_creation: int
    total: int
    runs: int
    usage_limit_hits: int
    runs_hit_usage_limit: int


class UsageGroup(UsageTotals):
    """One row of `factory usage`: `key` is `<repo>/<run ID>`, the repo, the role or the UTC day
    (`YYYY-MM-DD`). Grouped by run, `repo`, `run_id` and `slug` name the run; else they are None.
    """

    key: str
    repo: str | None = None
    run_id: int | None = None
    slug: str | None = None


class Usage(_Model):
    """`factory usage`'s answer: the groups in order and their total. `repo` None: every repo;
    `since`: the first UTC day counted, None for all of them."""

    by: Grouping
    repo: str | None
    since: date | None
    groups: list[UsageGroup]
    total: UsageTotals


def read_usage(registry: Registry, *, by: Grouping, repo: str | None, since: date | None) -> Usage:
    """Usage for `repo` (None: every repo) from `since` (a UTC day, inclusive) on, by `by`."""
    return summarize(registry.list_usage(repo), by=by, repo=repo, since=since)


def summarize(
    records: UsageRecords, *, by: Grouping, repo: str | None, since: date | None
) -> Usage:
    """Total `records` by `by`, keeping only `repo`'s (None: all) from the UTC day `since` on."""

    def wanted(row: TokenUseRow | PauseRow) -> bool:
        return (repo is None or row.repo == repo) and (since is None or _day(row.at) >= since)

    uses = [row for row in records.token_use if wanted(row)]
    hits = [row for row in records.pauses if wanted(row) and row.reason == "usage_limit"]
    groups: dict[_Key, _Sum] = {}
    everything = _Sum()
    for row in uses:
        groups.setdefault(_key(by, row), _Sum()).add_turn(row)
        everything.add_turn(row)
    for row in hits:
        groups.setdefault(_key(by, row), _Sum()).add_hit(row)
        everything.add_hit(row)
    return Usage(
        by=by,
        repo=repo,
        since=since,
        groups=[_group(by, key, groups[key], records.slugs) for key in sorted(groups)],
        total=everything.totals(),
    )


# ------------------------------------------------------------------ helpers

type _Key = tuple[str | int, ...]


@dataclass
class _Sum:
    turns: int = 0
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_creation: int = 0
    runs: set[tuple[str, int]] = field(default_factory=set[tuple[str, int]])
    hits: int = 0
    runs_hit: set[tuple[str, int]] = field(default_factory=set[tuple[str, int]])

    def add_turn(self, row: TokenUseRow) -> None:
        self.turns += 1
        self.input += row.input
        self.output += row.output
        self.cache_read += row.cache_read
        self.cache_creation += row.cache_creation
        self.runs.add((row.repo, row.run_id))

    def add_hit(self, row: PauseRow) -> None:
        self.hits += 1
        self.runs.add((row.repo, row.run_id))
        self.runs_hit.add((row.repo, row.run_id))

    def totals(self) -> UsageTotals:
        return UsageTotals(
            turns=self.turns,
            input=self.input,
            output=self.output,
            cache_read=self.cache_read,
            cache_creation=self.cache_creation,
            total=self.input + self.output + self.cache_read + self.cache_creation,
            runs=len(self.runs),
            usage_limit_hits=self.hits,
            runs_hit_usage_limit=len(self.runs_hit),
        )


def _day(at: datetime) -> date:
    return at.astimezone(UTC).date()


def _key(by: Grouping, row: TokenUseRow | PauseRow) -> _Key:
    """The row's group, as a key that sorts runs by repo and ID, repos by name, roles in
    pipeline order and days in time order."""
    if by == "run":
        return (row.repo, row.run_id)
    if by == "repo":
        return (row.repo,)
    if by == "role":
        return (ROLE_ORDER.index(row.role), row.role)
    return (_day(row.at).isoformat(),)


def _group(by: Grouping, key: _Key, sums: _Sum, slugs: dict[tuple[str, int], str]) -> UsageGroup:
    counts = sums.totals().model_dump()
    if by == "run":
        repo, run_id = str(key[0]), int(key[1])
        return UsageGroup(
            key=f"{repo}/{run_id}",
            repo=repo,
            run_id=run_id,
            slug=slugs.get((repo, run_id)),
            **counts,
        )
    return UsageGroup(key=str(key[-1]), **counts)
