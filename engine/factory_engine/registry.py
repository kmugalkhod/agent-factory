"""The SQLite registry: repos, an index over every run's `run.json`, and agent sessions.

`run.json` stays the source of truth for a run. The `runs` and `sessions` tables hold everything
in a `Run`, so `get_run` returns a `Run` equal to the file, and `rebuild_index` can recreate both
tables from the run folders. Repos are the registry's own data: run folders don't record a
repo's path or readiness.

One `sessions` row per run and role that has a session ID, an attempt count or a token total.
"""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict

from factory_engine.errors import RegistryError
from factory_engine.paths import repo_runs_dir, run_dir
from factory_engine.run import CLAIMS, RUN_FILE, Metrics, Role, Run, load_run

REGISTRY_FILE = "registry.db"

# MIGRATIONS[n] takes the schema from version n to n + 1.
MIGRATIONS = [
    """
    CREATE TABLE repos (
        name TEXT PRIMARY KEY,
        path TEXT NOT NULL,
        ready INTEGER NOT NULL CHECK (ready IN (0, 1)),
        added_at TEXT NOT NULL
    );
    CREATE TABLE runs (
        repo TEXT NOT NULL,
        id INTEGER NOT NULL CHECK (id >= 0),
        slug TEXT NOT NULL,
        kind TEXT NOT NULL,
        state TEXT NOT NULL,
        stage TEXT,
        provider TEXT NOT NULL,
        isolation TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_completed_step TEXT,
        important_findings INTEGER NOT NULL,
        pr_lines_changed_by_you INTEGER,
        PRIMARY KEY (repo, id)
    );
    CREATE TABLE sessions (
        repo TEXT NOT NULL,
        run_id INTEGER NOT NULL,
        role TEXT NOT NULL,
        session_id TEXT,
        attempts INTEGER,
        tokens INTEGER,
        PRIMARY KEY (repo, run_id, role),
        FOREIGN KEY (repo, run_id) REFERENCES runs (repo, id) ON DELETE CASCADE
    );
    CREATE INDEX runs_by_state ON runs (state);
    """,
]
SCHEMA_VERSION = len(MIGRATIONS)

RUN_COLUMNS = (
    "repo, id, slug, kind, state, stage, provider, isolation, created_at, updated_at, "
    "last_completed_step, important_findings, pr_lines_changed_by_you"
)
ROLES: tuple[Role, ...] = ("planner", "tester", "builder", "reviewer")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Repo(_Model):
    name: str
    path: Path
    ready: bool
    added_at: AwareDatetime


class Session(_Model):
    repo: str
    run_id: int
    role: Role
    session_id: str | None
    attempts: int | None
    tokens: int | None


def registry_path(data: Path) -> Path:
    return data / REGISTRY_FILE


@contextmanager
def open_registry(path: Path, timeout: float = 5.0) -> Iterator["Registry"]:
    """Open (creating or migrating) the registry at `path`, and close it afterwards."""
    registry = Registry(path, timeout)
    try:
        yield registry
    finally:
        registry.close()


class Registry:
    def __init__(self, path: Path, timeout: float = 5.0) -> None:
        self.path = path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(path, timeout=timeout, isolation_level=None)
        except (OSError, sqlite3.Error) as err:
            raise RegistryError(f"{path}: can't open the registry ({err}).") from err
        try:
            self._migrate()
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.Error as err:
            self._conn.close()
            raise RegistryError(f"{path}: can't set up the registry ({err}).") from err
        except BaseException:
            self._conn.close()
            raise

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------ plumbing

    @contextmanager
    def _write(self, action: str) -> Iterator[sqlite3.Connection]:
        """One write transaction. Any SQLite error rolls it back and becomes a RegistryError."""
        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as err:
            raise RegistryError(f"{self.path}: can't {action} ({err}). Retry.") from err
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except BaseException as err:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            if isinstance(err, sqlite3.Error):
                raise RegistryError(f"{self.path}: can't {action} ({err}).") from err
            raise

    @contextmanager
    def _snapshot(self) -> Iterator[None]:
        """One read transaction, so several queries see the same committed version."""
        if self._conn.in_transaction:
            yield
            return
        try:
            self._conn.execute("BEGIN")
        except sqlite3.Error as err:
            raise RegistryError(f"{self.path}: can't read the registry ({err}).") from err
        try:
            yield
        finally:
            if self._conn.in_transaction:
                self._conn.execute("COMMIT")

    def _read(self, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        try:
            cursor = self._conn.execute(sql, params)
            cursor.row_factory = sqlite3.Row
            return cursor.fetchall()
        except sqlite3.Error as err:
            raise RegistryError(f"{self.path}: can't read the registry ({err}).") from err

    def _migrate(self) -> None:
        """Bring the schema to SCHEMA_VERSION.

        A current schema needs no write, so the first check takes no lock and opens don't wait
        for writers. A migration takes the write lock and checks the version again, since
        another open may have migrated in between.
        """
        version = _schema_version(self._conn)
        self._check_supported(version)
        if version == SCHEMA_VERSION:
            return
        with self._write("migrate the schema") as conn:
            version = _schema_version(conn)
            self._check_supported(version)
            for step in range(version, SCHEMA_VERSION):
                for statement in MIGRATIONS[step].split(";"):
                    if statement.strip():
                        conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {step + 1}")

    def _check_supported(self, version: int) -> None:
        if version > SCHEMA_VERSION:
            raise RegistryError(
                f"{self.path}: schema version {version} is newer than this factory "
                f"supports ({SCHEMA_VERSION}). Upgrade the factory before using it."
            )

    # --------------------------------------------------------------- repos

    def add_repo(self, name: str, path: Path, *, now: datetime) -> Repo:
        repo_runs_dir(Path(), name)  # the name becomes a folder name; refuse unsafe ones
        repo = Repo(name=name, path=path.resolve(), ready=False, added_at=now)
        with self._write(f"add repo {name!r}") as conn:
            if _repo_in(conn, name) is not None:
                raise RegistryError(
                    f"repo {name!r} is already registered. Use update_repo instead."
                )
            conn.execute(
                "INSERT INTO repos (name, path, ready, added_at) VALUES (?, ?, ?, ?)",
                (repo.name, str(repo.path), int(repo.ready), repo.added_at.isoformat()),
            )
        return repo

    def get_repo(self, name: str) -> Repo | None:
        try:
            return _repo_in(self._conn, name)
        except sqlite3.Error as err:
            raise RegistryError(f"{self.path}: can't read the registry ({err}).") from err

    def list_repos(self) -> list[Repo]:
        return [_repo(row) for row in self._read("SELECT * FROM repos ORDER BY name")]

    def update_repo(
        self, name: str, *, path: Path | None = None, ready: bool | None = None
    ) -> Repo:
        with self._write(f"update repo {name!r}") as conn:
            current = _repo_in(conn, name)  # read under the lock, so no update is lost
            if current is None:
                raise RegistryError(f"repo {name!r} is not registered. Add it with add_repo first.")
            updated = current.model_copy(
                update={
                    "path": path.resolve() if path is not None else current.path,
                    "ready": ready if ready is not None else current.ready,
                }
            )
            conn.execute(
                "UPDATE repos SET path = ?, ready = ? WHERE name = ?",
                (str(updated.path), int(updated.ready), name),
            )
        return updated

    # ---------------------------------------------------- runs and sessions

    def upsert_run(self, run: Run) -> None:
        """Index `run` (call after `save_run`). Replaces any earlier row and its sessions."""
        with self._write(f"index run {run.repo}/{run.id}") as conn:
            _insert_run(conn, run)

    def get_run(self, repo: str, run_id: int) -> Run | None:
        with self._snapshot():  # the run row and its sessions from one committed version
            rows = self._read(
                f"SELECT {RUN_COLUMNS} FROM runs WHERE repo = ? AND id = ?", (repo, run_id)
            )
            return self._run(rows[0]) if rows else None

    def list_runs(self, repo: str | None = None) -> list[Run]:
        with self._snapshot():
            if repo is None:
                rows = self._read(f"SELECT {RUN_COLUMNS} FROM runs ORDER BY repo, id")
            else:
                rows = self._read(
                    f"SELECT {RUN_COLUMNS} FROM runs WHERE repo = ? ORDER BY id", (repo,)
                )
            return [self._run(row) for row in rows]

    def list_sessions(self, repo: str | None = None, run_id: int | None = None) -> list[Session]:
        where, params = [], []
        if repo is not None:
            where.append("repo = ?")
            params.append(repo)
        if run_id is not None:
            where.append("run_id = ?")
            params.append(run_id)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        rows = self._read(
            f"SELECT * FROM sessions{clause} ORDER BY repo, run_id, role", tuple(params)
        )
        return [Session.model_validate(dict(row)) for row in rows]

    def rebuild_index(self, data: Path) -> int:
        """Recreate `runs` and `sessions` from every `run.json` under `<data>/runs/`.

        All or nothing: any bad or misplaced file raises and leaves the index as it was.
        Folders without a `run.json` (allocated, never saved) are skipped. Files are read under
        the write lock, so a concurrent `upsert_run` can't be overwritten by an older copy.
        Returns the number of runs indexed.
        """
        with self._write("rebuild the run index") as conn:
            runs = [_load_checked(data, folder) for folder in _run_folders(data)]
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM runs")
            for run in runs:
                _insert_run(conn, run)
        return len(runs)

    def _run(self, row: sqlite3.Row) -> Run:
        sessions = self.list_sessions(row["repo"], row["id"])
        return Run(
            id=row["id"],
            slug=row["slug"],
            repo=row["repo"],
            kind=row["kind"],
            state=row["state"],
            stage=row["stage"],
            attempts={s.role: s.attempts for s in sessions if s.attempts is not None},
            session_ids={s.role: s.session_id for s in sessions if s.session_id is not None},
            provider=row["provider"],
            isolation=row["isolation"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            metrics=Metrics(
                tokens={s.role: s.tokens for s in sessions if s.tokens is not None},
                important_findings=row["important_findings"],
                pr_lines_changed_by_you=row["pr_lines_changed_by_you"],
            ),
            last_completed_step=row["last_completed_step"],
        )


def _schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _repo_in(conn: sqlite3.Connection, name: str) -> Repo | None:
    cursor = conn.execute("SELECT * FROM repos WHERE name = ?", (name,))
    cursor.row_factory = sqlite3.Row
    row = cursor.fetchone()
    return _repo(row) if row is not None else None


def _repo(row: sqlite3.Row) -> Repo:
    return Repo(
        name=row["name"],
        path=Path(row["path"]),
        ready=bool(row["ready"]),
        added_at=datetime.fromisoformat(row["added_at"]),
    )


def _insert_run(conn: sqlite3.Connection, run: Run) -> None:
    conn.execute("DELETE FROM runs WHERE repo = ? AND id = ?", (run.repo, run.id))  # cascades
    conn.execute(
        f"INSERT INTO runs ({RUN_COLUMNS}) VALUES ({', '.join('?' * 13)})",
        (
            run.repo,
            run.id,
            run.slug,
            run.kind,
            run.state,
            run.stage,
            run.provider,
            run.isolation,
            run.created_at.isoformat(),
            run.updated_at.isoformat(),
            run.last_completed_step,
            run.metrics.important_findings,
            run.metrics.pr_lines_changed_by_you,
        ),
    )
    for role in ROLES:
        values = (run.session_ids.get(role), run.attempts.get(role), run.metrics.tokens.get(role))
        if any(value is not None for value in values):
            conn.execute(
                "INSERT INTO sessions (repo, run_id, role, session_id, attempts, tokens) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run.repo, run.id, role, *values),
            )


def _run_folders(data: Path) -> list[Path]:
    runs = data / "runs"
    if not runs.is_dir():
        return []
    return sorted(
        folder
        for repo in runs.iterdir()
        if repo.is_dir()
        for folder in repo.iterdir()
        if folder.is_dir() and folder.name != CLAIMS and (folder / RUN_FILE).is_file()
    )  # names are checked by _load_checked, so a renamed folder is reported, not skipped


def _load_checked(data: Path, folder: Path) -> Run:
    """Load a run and check it belongs in this folder: same repo, ID and slug."""
    run = load_run(folder)
    if not _same_folder(run_dir(data, run.repo, run.id, run.slug), folder):
        raise RegistryError(
            f"{folder}: run.json says {run.repo}/{run.id}-{run.slug}, which belongs in a "
            "different folder. Fix the file or move the folder, then rebuild again."
        )
    return run


def _same_folder(a: Path, b: Path) -> bool:
    """Compare resolved paths with the platform's case rules (CLAUDE.md: Windows paths)."""
    return os.path.normcase(a.resolve()) == os.path.normcase(b.resolve())
