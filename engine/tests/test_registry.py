"""Tests for factory_engine.registry (task 2.3): repos, the run index and agent sessions."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from factory_engine.errors import DataFolderError, RegistryError
from factory_engine.registry import (
    SCHEMA_VERSION,
    Registry,
    Session,
    open_registry,
    registry_path,
)
from factory_engine.run import Metrics, Run, allocate_run, new_run, save_run

NOW = datetime(2026, 10, 10, 9, 30, tzinfo=UTC)


def _run(repo: str, run_id: int, slug: str, **changes: object) -> Run:
    run = new_run(
        run_id=run_id,
        slug=slug,
        repo=repo,
        kind="feature",
        provider="subscription",
        isolation="worktree",
        now=NOW,
    )
    return run.model_copy(update=changes)


def _busy_run(repo: str, run_id: int, slug: str) -> Run:
    return _run(
        repo,
        run_id,
        slug,
        state="building",
        stage="builder",
        attempts={"planner": 1, "tester": 1, "builder": 2},
        session_ids={"planner": "s-plan", "builder": "s-build"},
        updated_at=NOW + timedelta(minutes=5),
        last_completed_step="tester_gate",
        metrics=Metrics(tokens={"planner": 500, "reviewer": 40}, important_findings=1),
    )


@pytest.fixture
def reg(tmp_path: Path) -> Iterator[Registry]:
    with open_registry(tmp_path / "registry.db") as registry:
        yield registry


def _dump(path: Path) -> dict[str, list[tuple[object, ...]]]:
    """Every row of every index table, in a stable order."""
    with sqlite3.connect(path) as conn:
        return {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2, 3").fetchall()
            for table in ("runs", "sessions")
        }


# ------------------------------------------------------------------ schema


def test_registry_path_is_in_the_data_folder(tmp_path: Path) -> None:
    assert registry_path(tmp_path) == tmp_path / "registry.db"


def test_new_database_is_migrated_from_version_0(tmp_path: Path) -> None:
    path = tmp_path / "registry.db"
    sqlite3.connect(path).close()  # an empty file: user_version 0
    with open_registry(path):
        pass
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"repos", "runs", "sessions"} <= tables


def test_reopening_keeps_the_data(tmp_path: Path) -> None:
    path = tmp_path / "registry.db"
    with open_registry(path) as reg:
        reg.add_repo("app", tmp_path / "app", now=NOW)
    with open_registry(path) as reg:
        assert [r.name for r in reg.list_repos()] == ["app"]


def test_newer_schema_on_disk_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "registry.db"
    with sqlite3.connect(path) as conn:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(RegistryError) as exc:
        open_registry(path).__enter__()
    message = str(exc.value)
    assert str(path) in message
    assert str(SCHEMA_VERSION + 1) in message
    assert "newer" in message


def test_database_uses_wal(tmp_path: Path) -> None:
    path = tmp_path / "registry.db"
    with open_registry(path):
        pass
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_unreadable_database_raises_registry_error(tmp_path: Path) -> None:
    path = tmp_path / "registry.db"
    path.write_bytes(b"this is not a sqlite database" * 100)
    with pytest.raises(RegistryError) as exc:
        open_registry(path).__enter__()
    assert str(path) in str(exc.value)


# ------------------------------------------------------------------- repos


def test_add_get_list_and_update_repos(reg: Registry, tmp_path: Path) -> None:  # AC1
    added = reg.add_repo("app", tmp_path / "app", now=NOW)
    reg.add_repo("api", tmp_path / "api", now=NOW)
    assert added.name == "app"
    assert added.path == (tmp_path / "app").resolve()
    assert added.ready is False
    assert added.added_at == NOW
    assert [r.name for r in reg.list_repos()] == ["api", "app"]

    updated = reg.update_repo("app", ready=True, path=tmp_path / "moved")
    assert updated.ready is True
    assert updated.path == (tmp_path / "moved").resolve()
    assert reg.get_repo("app") == updated
    assert reg.get_repo("missing") is None


def test_adding_a_repo_twice_is_refused(reg: Registry, tmp_path: Path) -> None:
    reg.add_repo("app", tmp_path, now=NOW)
    with pytest.raises(RegistryError) as exc:
        reg.add_repo("app", tmp_path, now=NOW)
    assert "app" in str(exc.value)


def test_updating_an_unknown_repo_is_refused(reg: Registry) -> None:
    with pytest.raises(RegistryError) as exc:
        reg.update_repo("ghost", ready=True)
    assert "ghost" in str(exc.value)


def test_unsafe_repo_name_is_refused(reg: Registry, tmp_path: Path) -> None:
    with pytest.raises(DataFolderError, match="repo name"):
        reg.add_repo("../escape", tmp_path, now=NOW)


# -------------------------------------------------------- runs and sessions


def test_upsert_then_get_gives_an_equal_run(reg: Registry) -> None:  # AC1
    run = _busy_run("app", 3, "status-json")
    reg.upsert_run(run)
    assert reg.get_run("app", 3) == run
    assert reg.get_run("app", 4) is None


def test_upsert_updates_the_run_and_its_sessions(reg: Registry) -> None:  # AC1
    reg.upsert_run(_busy_run("app", 3, "status-json"))
    later = _busy_run("app", 3, "status-json").model_copy(
        update={
            "state": "reviewing",
            "stage": "reviewer",
            "session_ids": {"builder": "s-build-2", "reviewer": "s-rev"},
            "attempts": {"builder": 3},
            "metrics": Metrics(),
        }
    )
    reg.upsert_run(later)
    assert reg.get_run("app", 3) == later
    assert reg.list_sessions("app", 3) == [
        Session(
            repo="app", run_id=3, role="builder", session_id="s-build-2", attempts=3, tokens=None
        ),
        Session(
            repo="app", run_id=3, role="reviewer", session_id="s-rev", attempts=None, tokens=None
        ),
    ]


def test_list_runs_filters_by_repo(reg: Registry) -> None:  # AC1
    for repo, run_id in [("app", 1), ("api", 0), ("app", 0)]:
        reg.upsert_run(_run(repo, run_id, f"task-{run_id}"))
    assert [(r.repo, r.id) for r in reg.list_runs()] == [("api", 0), ("app", 0), ("app", 1)]
    assert [(r.repo, r.id) for r in reg.list_runs("app")] == [("app", 0), ("app", 1)]


def test_sessions_hold_session_id_attempts_and_tokens_per_role(reg: Registry) -> None:  # AC1
    reg.upsert_run(_busy_run("app", 3, "status-json"))
    assert reg.list_sessions("app", 3) == [
        Session(
            repo="app", run_id=3, role="builder", session_id="s-build", attempts=2, tokens=None
        ),
        Session(repo="app", run_id=3, role="planner", session_id="s-plan", attempts=1, tokens=500),
        Session(repo="app", run_id=3, role="reviewer", session_id=None, attempts=None, tokens=40),
        Session(repo="app", run_id=3, role="tester", session_id=None, attempts=1, tokens=None),
    ]
    assert reg.list_sessions() == reg.list_sessions("app", 3)


# ----------------------------------------------------------------- rebuild


def _seed_runs(data: Path, reg: Registry) -> None:
    """Create run folders on disk and index each run the way the engine would."""
    for repo, slug, busy in [
        ("app", "bootstrap", False),
        ("app", "add-login", True),
        ("api", "x", True),
    ]:
        run_id, folder = allocate_run(data, repo, slug)
        run = _busy_run(repo, run_id, slug) if busy else _run(repo, run_id, slug)
        save_run(run, folder)
        reg.upsert_run(run)


def test_rebuild_reproduces_the_index_exactly(tmp_path: Path) -> None:  # AC3
    data = tmp_path / "data"
    path = registry_path(data)
    data.mkdir()
    with open_registry(path) as reg:
        _seed_runs(data, reg)
    before = _dump(path)
    assert before["runs"] and before["sessions"]

    with open_registry(path) as reg:
        assert reg.rebuild_index(data) == 3
    assert _dump(path) == before


def test_rebuild_into_a_fresh_database_matches(tmp_path: Path) -> None:  # AC3
    data = tmp_path / "data"
    data.mkdir()
    with open_registry(registry_path(data)) as reg:
        _seed_runs(data, reg)
    fresh = tmp_path / "fresh.db"
    with open_registry(fresh) as reg:
        reg.rebuild_index(data)
    assert _dump(fresh) == _dump(registry_path(data))


def test_rebuild_drops_runs_whose_folder_is_gone(tmp_path: Path) -> None:  # AC3
    data = tmp_path / "data"
    data.mkdir()
    with open_registry(registry_path(data)) as reg:
        _seed_runs(data, reg)
        reg.upsert_run(_busy_run("app", 9, "ghost"))  # indexed, but no folder
        reg.rebuild_index(data)
        assert reg.get_run("app", 9) is None
        assert reg.list_sessions("app", 9) == []


def test_rebuild_skips_folders_without_run_json(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    allocate_run(data, "app", "allocated-only")  # crashed before the first save
    with open_registry(registry_path(data)) as reg:
        assert reg.rebuild_index(data) == 0


def test_rebuild_refuses_a_run_json_that_doesnt_match_its_folder(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _, folder = allocate_run(data, "app", "real")
    save_run(_run("app", 7, "other"), folder)
    with open_registry(registry_path(data)) as reg:
        reg.upsert_run(_run("app", 1, "kept"))
        with pytest.raises(RegistryError) as exc:
            reg.rebuild_index(data)
        assert str(folder) in str(exc.value)
        assert reg.get_run("app", 1) is not None  # a failed rebuild leaves the index unchanged


# ------------------------------------------------------------- concurrency


def test_two_connections_read_while_one_writes(tmp_path: Path) -> None:  # AC4
    path = tmp_path / "registry.db"
    with open_registry(path) as reg:
        reg.upsert_run(_run("app", 0, "first"))

    old_reader = sqlite3.connect(path, timeout=0.2, isolation_level=None)
    old_reader.execute("BEGIN")
    assert old_reader.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1  # read snapshot

    with open_registry(path, timeout=0.2) as writer:
        writer.upsert_run(_run("app", 1, "second"))  # commits while the reader is open

        with open_registry(path, timeout=0.2) as new_reader:
            assert [r.id for r in new_reader.list_runs()] == [0, 1]
        assert old_reader.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    old_reader.execute("COMMIT")
    old_reader.close()
