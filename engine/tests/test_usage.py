"""Tests for factory_engine.usage (task 2.29): token totals by run, repo, role and day, read
from a seeded registry, with how often runs hit the usage limit."""

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from factory_engine.registry import Registry, open_registry
from factory_engine.run import Metrics, PauseReason, PauseRecord, Role, Run, TokenUse, new_run
from factory_engine.usage import GROUPINGS, Grouping, Usage, UsageGroup, UsageTotals, read_usage

DAY1 = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
DAY2 = DAY1 + timedelta(days=1)
DAY3 = DAY1 + timedelta(days=2)


def use(role: Role, attempt: int, at: datetime, i: int, o: int, r: int, c: int) -> TokenUse:
    return TokenUse(
        role=role, attempt=attempt, at=at, input=i, output=o, cache_read=r, cache_creation=c
    )


def pause(reason: PauseReason, role: Role, at: datetime) -> PauseRecord:
    return PauseRecord(reason=reason, role=role, attempt=1, at=at, resume_at=None)


def seeded_run(
    repo: str, run_id: int, slug: str, uses: list[TokenUse], pauses: list[PauseRecord]
) -> Run:
    run = new_run(
        run_id=run_id,
        slug=slug,
        repo=repo,
        kind="feature",
        provider="subscription",
        isolation="worktree",
        now=DAY1,
    )
    return run.model_copy(update={"metrics": Metrics(token_use=uses, pauses=pauses)})


SEED = [
    seeded_run(
        "pilot",
        1,
        "add-mul",
        [
            use("planner", 1, DAY1, 100, 10, 1000, 50),
            use("builder", 1, DAY1 + timedelta(hours=1), 200, 20, 2000, 0),
            use("builder", 2, DAY2, 300, 30, 3000, 100),
        ],
        [
            pause("usage_limit", "builder", DAY1 + timedelta(hours=2)),
            pause("turn_cap", "planner", DAY1),
        ],
    ),
    seeded_run(
        "pilot",
        2,
        "fix-div",
        [use("reviewer", 1, DAY2, 50, 5, 500, 10)],
        [pause("usage_limit", "reviewer", DAY2), pause("usage_limit", "reviewer", DAY2)],
    ),
    seeded_run("other", 1, "docs", [use("planner", 1, DAY3, 10, 1, 100, 0)], []),
]


@pytest.fixture
def reg(tmp_path: Path) -> Iterator[Registry]:
    with open_registry(tmp_path / "registry.db") as registry:
        for run in SEED:
            registry.upsert_run(run)
        yield registry


def totals(
    turns: int,
    tokens: tuple[int, int, int, int],
    *,
    runs: int = 1,
    hits: int = 0,
    runs_hit: int = 0,
) -> dict[str, int]:
    i, o, r, c = tokens
    return {
        "turns": turns,
        "input": i,
        "output": o,
        "cache_read": r,
        "cache_creation": c,
        "total": i + o + r + c,
        "runs": runs,
        "usage_limit_hits": hits,
        "runs_hit_usage_limit": runs_hit,
    }


PILOT_1 = totals(3, (600, 60, 6000, 150), hits=1, runs_hit=1)
PILOT_2 = totals(1, (50, 5, 500, 10), hits=2, runs_hit=1)
OTHER_1 = totals(1, (10, 1, 100, 0))


def counts(group: UsageGroup | UsageTotals) -> UsageTotals:
    return UsageTotals.model_validate(group.model_dump(include=set(UsageTotals.model_fields)))


@pytest.mark.parametrize(
    ("by", "repo", "expected"),
    [
        ("run", "pilot", [("pilot/1", PILOT_1), ("pilot/2", PILOT_2)]),
        ("run", None, [("other/1", OTHER_1), ("pilot/1", PILOT_1), ("pilot/2", PILOT_2)]),
        (
            "repo",
            None,
            [
                ("other", OTHER_1),
                ("pilot", totals(4, (650, 65, 6500, 160), runs=2, hits=3, runs_hit=2)),
            ],
        ),
        (
            "role",
            None,  # in pipeline order, not by name; the turn cap isn't a usage-limit hit
            [
                ("planner", totals(2, (110, 11, 1100, 50), runs=2)),
                ("builder", totals(2, (500, 50, 5000, 100), hits=1, runs_hit=1)),
                ("reviewer", totals(1, (50, 5, 500, 10), hits=2, runs_hit=1)),
            ],
        ),
        (
            "day",
            "pilot",
            [
                ("2026-10-14", totals(2, (300, 30, 3000, 50), hits=1, runs_hit=1)),
                ("2026-10-15", totals(2, (350, 35, 3500, 110), runs=2, hits=2, runs_hit=1)),
            ],
        ),
    ],
)
def test_every_grouping_returns_the_expected_totals(
    reg: Registry, by: Grouping, repo: str | None, expected: list[tuple[str, dict[str, int]]]
) -> None:
    usage = read_usage(reg, by=by, repo=repo, since=None)

    assert [(g.key, counts(g)) for g in usage.groups] == [
        (key, UsageTotals.model_validate(values)) for key, values in expected
    ]


@pytest.mark.parametrize(
    ("repo", "expected"),
    [
        ("pilot", totals(4, (650, 65, 6500, 160), runs=2, hits=3, runs_hit=2)),
        (None, totals(5, (660, 66, 6600, 160), runs=3, hits=3, runs_hit=2)),
    ],
)
def test_the_total_covers_every_group(
    reg: Registry, repo: str | None, expected: dict[str, int]
) -> None:
    for by in GROUPINGS:
        usage = read_usage(reg, by=by, repo=repo, since=None)
        assert usage.total == UsageTotals.model_validate(expected)


def test_a_run_group_names_its_repo_id_and_slug(reg: Registry) -> None:
    usage = read_usage(reg, by="run", repo="pilot", since=None)

    assert [(g.repo, g.run_id, g.slug) for g in usage.groups] == [
        ("pilot", 1, "add-mul"),
        ("pilot", 2, "fix-div"),
    ]
    assert (usage.by, usage.repo, usage.since) == ("run", "pilot", None)


def test_since_keeps_turns_and_pauses_from_that_utc_day_on(reg: Registry) -> None:
    usage = read_usage(reg, by="run", repo="pilot", since=date(2026, 10, 15))

    assert [(g.key, counts(g)) for g in usage.groups] == [
        ("pilot/1", UsageTotals.model_validate(totals(1, (300, 30, 3000, 100)))),
        ("pilot/2", UsageTotals.model_validate(PILOT_2)),
    ]
    assert usage.since == date(2026, 10, 15)


def test_a_day_is_the_utc_day(tmp_path: Path) -> None:
    late = datetime(2026, 10, 14, 23, 30, tzinfo=timezone(timedelta(hours=-5)))  # 04:30 UTC
    with open_registry(tmp_path / "registry.db") as reg:
        reg.upsert_run(seeded_run("pilot", 1, "late", [use("planner", 1, late, 1, 0, 0, 0)], []))

        usage = read_usage(reg, by="day", repo=None, since=None)

    assert [g.key for g in usage.groups] == ["2026-10-15"]


def test_a_repo_with_no_records_has_no_groups_and_zero_totals(reg: Registry) -> None:
    usage = read_usage(reg, by="role", repo="nobody", since=None)

    assert usage.groups == []
    assert usage.total == UsageTotals.model_validate(totals(0, (0, 0, 0, 0), runs=0))


def test_usage_round_trips_through_json(reg: Registry) -> None:
    usage = read_usage(reg, by="day", repo=None, since=date(2026, 10, 14))

    assert Usage.model_validate_json(usage.model_dump_json()) == usage
