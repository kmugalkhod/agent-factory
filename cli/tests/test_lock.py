"""Tests for factory_cli.lock (task 2.28): one factory process drives a run at a time."""

from pathlib import Path

import pytest

from factory_cli.errors import CliError
from factory_cli.lock import RunLock


def test_a_held_lock_refuses_a_second_holder(tmp_path: Path) -> None:
    first, second = RunLock(tmp_path / "engine.lock"), RunLock(tmp_path / "engine.lock")
    first.acquire()
    try:
        assert second.held()
        with pytest.raises(CliError, match="another factory process"):
            second.acquire()
    finally:
        first.release()


def test_a_released_lock_can_be_taken_again(tmp_path: Path) -> None:
    lock = RunLock(tmp_path / "engine.lock")
    lock.acquire()
    lock.release()

    assert not RunLock(tmp_path / "engine.lock").held()
    again = RunLock(tmp_path / "engine.lock")
    again.acquire()
    again.release()


def test_checking_a_lock_never_creates_the_file(tmp_path: Path) -> None:
    path = tmp_path / "engine.lock"

    assert not RunLock(path).held()
    assert not path.exists()


def test_the_lock_works_as_a_context_manager(tmp_path: Path) -> None:
    path = tmp_path / "engine.lock"

    with RunLock(path):
        assert RunLock(path).held()
    assert not RunLock(path).held()
    assert path.exists()  # kept, so no folder changes while another run's stage watches it
