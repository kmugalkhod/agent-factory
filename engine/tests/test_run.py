"""Tests for factory_engine.run (task 2.2): the Run model, run.json and run ID allocation."""

import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from factory_engine import run as run_module
from factory_engine.errors import DataFolderError, RunFileError
from factory_engine.paths import run_dir
from factory_engine.run import Metrics, Run, allocate_run, load_run, new_run, save_run

NOW = datetime(2026, 10, 10, 9, 30, tzinfo=UTC)


def _run(**changes: object) -> Run:
    run = new_run(
        run_id=3,
        slug="status-json",
        repo="agent-factory",
        kind="feature",
        provider="subscription",
        isolation="worktree",
        now=NOW,
    )
    return run.model_copy(update=changes)


def test_new_run_starts_pending_with_nothing_done() -> None:
    run = _run()
    assert run.state == "pending"
    assert run.stage is None
    assert run.attempts == {}
    assert run.session_ids == {}
    assert run.last_completed_step is None
    assert run.created_at == run.updated_at == NOW
    assert run.metrics == Metrics()


def test_save_then_load_gives_an_equal_run(tmp_path: Path) -> None:  # AC1
    run = _run(
        state="building",
        stage="builder",
        attempts={"planner": 1, "tester": 1, "builder": 2},
        session_ids={"planner": "s-1", "tester": "s-2", "builder": "s-3"},
        updated_at=NOW + timedelta(minutes=20),
        last_completed_step="tester_gate",
        metrics=Metrics(tokens={"planner": 565_957, "builder": 947_069}, important_findings=1),
    )
    save_run(run, tmp_path)
    assert load_run(tmp_path) == run


def test_run_json_is_plain_utf8_json(tmp_path: Path) -> None:
    save_run(_run(), tmp_path)
    data = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert data["id"] == 3
    assert data["kind"] == "feature"


def test_interrupted_write_keeps_the_previous_run_json(  # AC2
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _run()
    save_run(first, tmp_path)

    def crash(src: object, dst: object) -> None:
        raise OSError("power cut before replace")

    monkeypatch.setattr(run_module.os, "replace", crash)
    with pytest.raises(RunFileError) as exc:
        save_run(_run(state="planning"), tmp_path)
    assert str(tmp_path / "run.json") in str(exc.value)
    monkeypatch.undo()

    assert load_run(tmp_path) == first
    assert sorted(p.name for p in tmp_path.iterdir()) == ["run.json"]


def test_leftover_temp_file_from_a_crash_doesnt_break_load(tmp_path: Path) -> None:  # AC2
    save_run(_run(), tmp_path)
    (tmp_path / "run.json.tmp").write_text('{"id": ', encoding="utf-8")
    assert load_run(tmp_path) == _run()


def test_save_into_a_missing_folder_names_the_file(tmp_path: Path) -> None:
    folder = tmp_path / "gone"
    with pytest.raises(RunFileError) as exc:
        save_run(_run(), folder)
    assert str(folder / "run.json") in str(exc.value)


def test_missing_run_json_names_the_file(tmp_path: Path) -> None:
    with pytest.raises(RunFileError) as exc:
        load_run(tmp_path)
    assert str(tmp_path / "run.json") in str(exc.value)


@pytest.mark.parametrize(
    "text",
    ['{"id": ', "[]", '{"id": 3}', '{"bogus": 1}'],
    ids=["truncated", "not-an-object", "missing-fields", "unknown-key"],
)
def test_invalid_run_json_names_the_file(tmp_path: Path, text: str) -> None:
    (tmp_path / "run.json").write_text(text, encoding="utf-8")
    with pytest.raises(RunFileError) as exc:
        load_run(tmp_path)
    assert str(tmp_path / "run.json") in str(exc.value)


def test_unknown_state_is_rejected(tmp_path: Path) -> None:
    save_run(_run(), tmp_path)
    path = tmp_path / "run.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["state"] = "dancing"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(RunFileError) as exc:
        load_run(tmp_path)
    assert "state" in str(exc.value)


def test_naive_timestamp_is_rejected() -> None:
    with pytest.raises(ValueError):
        new_run(
            run_id=0,
            slug="x",
            repo="r",
            kind="bootstrap",
            provider="subscription",
            isolation="worktree",
            now=datetime(2026, 10, 10, 9, 30),
        )


def test_run_ids_start_at_zero_and_increase_per_repo(tmp_path: Path) -> None:  # AC4
    first = allocate_run(tmp_path, "app", "bootstrap")
    second = allocate_run(tmp_path, "app", "add-login")
    other = allocate_run(tmp_path, "other", "bootstrap")
    third = allocate_run(tmp_path, "app", "add-login")
    assert [first[0], second[0], third[0]] == [0, 1, 2]
    assert other[0] == 0
    assert first[1] == run_dir(tmp_path, "app", 0, "bootstrap")
    assert third[1] == run_dir(tmp_path, "app", 2, "add-login")
    assert all(folder.is_dir() for _, folder in (first, second, third, other))


def test_run_ids_are_never_reused_after_a_folder_is_removed(tmp_path: Path) -> None:  # AC4
    _, folder = allocate_run(tmp_path, "app", "first")
    folder.rmdir()
    assert allocate_run(tmp_path, "app", "second")[0] == 1


def test_concurrent_allocation_gives_unique_ids(tmp_path: Path) -> None:  # AC4
    ids: list[int] = []
    lock = threading.Lock()

    def claim(n: int) -> None:
        run_id, _ = allocate_run(tmp_path, "app", f"task-{n}")
        with lock:
            ids.append(run_id)

    threads = [threading.Thread(target=claim, args=(n,)) for n in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(ids) == list(range(20))


def test_cleanup_failure_doesnt_hide_the_write_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR #12 review: an error deleting the temp file must not replace the RunFileError."""

    def crash(*args: object, **kwargs: object) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(run_module.os, "replace", crash)
    monkeypatch.setattr(Path, "unlink", crash)
    with pytest.raises(RunFileError) as exc:
        save_run(_run(), tmp_path)
    assert str(tmp_path / "run.json") in str(exc.value)


def test_allocate_when_data_folder_is_a_file_names_the_path(tmp_path: Path) -> None:
    """PR #12 review: disk failures in allocate_run raise DataFolderError, not OSError."""
    data = tmp_path / "data"
    data.write_text("not a folder", encoding="utf-8")
    with pytest.raises(DataFolderError) as exc:
        allocate_run(data, "app", "x")
    assert str(data) in str(exc.value)


def test_failed_id_claim_names_the_claim_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def deny(self: Path, *args: object, **kwargs: object) -> None:
        raise PermissionError("access denied")

    monkeypatch.setattr(Path, "touch", deny)
    with pytest.raises(DataFolderError) as exc:
        allocate_run(tmp_path, "app", "x")
    assert str(tmp_path / "runs" / "app" / ".ids") in str(exc.value)


def test_blocked_run_folder_names_the_folder(tmp_path: Path) -> None:
    blocker = run_dir(tmp_path, "app", 0, "x")
    blocker.parent.mkdir(parents=True)
    blocker.write_text("a file where the run folder goes", encoding="utf-8")
    with pytest.raises(DataFolderError) as exc:
        allocate_run(tmp_path, "app", "x")
    assert str(blocker) in str(exc.value)


def test_save_writes_through_a_temp_file_and_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC2: the only write to run.json is an os.replace
    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        calls.append((Path(src).name, Path(dst).name))
        real_replace(src, dst)

    monkeypatch.setattr(run_module.os, "replace", spy)
    save_run(_run(), tmp_path)
    assert len(calls) == 1
    assert calls[0][1] == "run.json"
    assert calls[0][0] != "run.json"
