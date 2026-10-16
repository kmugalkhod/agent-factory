"""One factory process drives a run at a time (the engine has one writer per run's event log).

The lock is an OS lock on `engine.lock` in the run folder, held while a process applies a
decision and runs the pipeline. The OS drops it when the process ends, so a crash or Ctrl-C never
leaves a run locked. The file is created once and then never changed or removed, and checking a
lock never creates it: a running stage's post-stage check watches the data folder.
"""

import sys
from pathlib import Path
from types import TracebackType
from typing import IO

from factory_cli.errors import CliError

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class RunLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: IO[bytes] | None = None

    def acquire(self) -> None:
        """Take the lock, or raise CliError when another process (or lock) holds it."""
        if self._handle is not None:
            return
        try:
            handle = self.path.open("ab")  # creates the file the first time; never written
        except OSError as err:
            raise CliError(
                f"{self.path}: can't open the run's lock ({err}). Check the run folder is writable."
            ) from err
        if not _try_lock(handle):
            handle.close()
            raise CliError(
                f"run {self.path.parent.name} is being driven by another factory process. Wait "
                "for it to stop, or press Ctrl-C in its terminal, then try again."
            )
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            _unlock(handle)
        finally:
            handle.close()

    def held(self) -> bool:
        """Whether some other holder has the lock now. Never creates the file; a lock that
        can't be checked counts as held."""
        try:
            handle = self.path.open("rb")
        except FileNotFoundError:
            return False
        except OSError:
            return True
        with handle:
            if not _try_lock(handle):
                return True
            _unlock(handle)
            return False

    def __enter__(self) -> "RunLock":
        self.acquire()
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        self.release()


def _try_lock(handle: IO[bytes]) -> bool:
    handle.seek(0)
    try:
        if sys.platform == "win32":
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle: IO[bytes]) -> None:
    handle.seek(0)
    if sys.platform == "win32":
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
