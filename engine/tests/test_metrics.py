"""Tests for factory_engine.metrics (task 2.27): the run's quality metrics, as pure updates of
`Metrics`, and the lines you changed on the pull request, measured on real git repos."""

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from factory_engine.git_ops import Git
from factory_engine.metrics import (
    builder_attempt,
    builder_gate,
    lines_changed,
    pause_hit,
    review_verdict,
    token_use,
    tokens_per_attempt,
    total_tokens,
    usage_limit_hits,
)
from factory_engine.run import Metrics, Pause, PauseRecord, Role, TokenUse

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


# ------------------------------------------------------------------ first pass


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        ([], None),
        ([("build", True)], None),  # passed its tests, not yet reviewed
        ([("build", True), ("review", True)], True),
        ([("build", True), ("review", False)], False),
        ([("build", False)], False),
        ([("build", False), ("build", True), ("review", True)], False),
        ([("build", True), ("review", False), ("build", True), ("review", True)], False),
        ([("build", True), ("review", True), ("build", False)], True),  # decided once
    ],
    ids=[
        "nothing-yet",
        "built-not-reviewed",
        "first-pass",
        "review-finds-important",
        "first-build-fails",
        "fails-then-passes",
        "review-then-fixed",
        "later-reruns-dont-change-it",
    ],
)
def test_first_pass_is_the_first_build_passing_tests_and_review(
    events: list[tuple[str, bool]], expected: bool | None
) -> None:
    metrics = Metrics()
    for kind, passed in events:
        metrics = (
            builder_gate(metrics, passed) if kind == "build" else review_verdict(metrics, passed)
        )
    assert metrics.first_pass is expected


@pytest.mark.parametrize(("attempt", "retries"), [(1, 0), (2, 1), (3, 2)])
def test_builder_retries_are_the_attempts_after_the_first(attempt: int, retries: int) -> None:
    assert builder_attempt(Metrics(), attempt).builder_retries == retries


def test_builder_retries_never_go_down() -> None:
    """A paused attempt carried on keeps its number; the count stays."""
    assert builder_attempt(builder_attempt(Metrics(), 3), 2).builder_retries == 2


# ------------------------------------------------------------------ tokens


def test_token_use_adds_a_dated_record_and_the_role_total() -> None:
    metrics = token_use(
        Metrics(), role="builder", attempt=2, at=NOW, input=10, output=5, cache_read=3
    )
    metrics = token_use(metrics, role="builder", attempt=2, at=NOW, input=1, output=1)
    assert metrics.tokens == {"builder": 20}
    assert metrics.token_use == [
        TokenUse(role="builder", attempt=2, at=NOW, input=10, output=5, cache_read=3),
        TokenUse(role="builder", attempt=2, at=NOW, input=1, output=1),
    ]


def test_tokens_per_attempt_and_total() -> None:
    metrics = Metrics()
    uses: list[tuple[Role, int, int]] = [
        ("planner", 1, 100),
        ("builder", 1, 300),
        ("builder", 2, 50),
        ("builder", 2, 60),
    ]
    for role, attempt, count in uses:
        metrics = token_use(metrics, role=role, attempt=attempt, at=NOW, input=count, output=0)
    assert tokens_per_attempt(metrics) == {
        ("planner", 1): 100,
        ("builder", 1): 300,
        ("builder", 2): 110,
    }
    assert total_tokens(metrics) == 510


# ------------------------------------------------------------------ pauses


def _pause(reason: str = "usage_limit") -> Pause:
    return Pause.model_validate(
        {
            "reason": reason,
            "detail": "limit",
            "state": "building",
            "role": "builder",
            "attempt": 2,
            "session_id": "s-1",
            "resume_at": (NOW + timedelta(hours=1)).isoformat()
            if reason == "usage_limit"
            else None,
        },
        strict=False,
    )


def test_a_pause_is_kept_after_the_run_unpauses() -> None:
    metrics = pause_hit(Metrics(), _pause(), at=NOW)
    metrics = pause_hit(metrics, _pause("turn_cap"), at=NOW + timedelta(minutes=5))
    assert metrics.pauses == [
        PauseRecord(
            reason="usage_limit",
            role="builder",
            attempt=2,
            at=NOW,
            resume_at=NOW + timedelta(hours=1),
        ),
        PauseRecord(
            reason="turn_cap",
            role="builder",
            attempt=2,
            at=NOW + timedelta(minutes=5),
            resume_at=None,
        ),
    ]
    assert usage_limit_hits(metrics) == 1


# ------------------------------------------------------------------ lines you changed


def sh(cwd: Path, *args: str, env: dict[str, str]) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        timeout=60,
        encoding="utf-8",
        errors="replace",
    )
    return done.stdout.strip()


class Repo:
    """A repo with `main` and a run branch, both local; `refs/remotes/origin/main` follows main
    by hand, as a fetch would."""

    def __init__(self, path: Path, env: dict[str, str]) -> None:
        self.path = path
        self.env = env
        path.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "core.autocrlf", "false")
        self.commit({"src/calc.py": "def add(a, b):\n    return a + b\n"}, "base")
        self.track()
        self.git("checkout", "-q", "-b", "run")

    def git(self, *args: str) -> str:
        return sh(self.path, *args, env=self.env)

    def commit(self, files: dict[str, str], message: str) -> str:
        for rel, text in files.items():
            target = self.path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode("utf-8"))
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def track(self) -> None:
        self.git("update-ref", "refs/remotes/origin/main", "main")

    def move_main(self, files: dict[str, str]) -> None:
        """Commit `files` on main, then rebase the run branch onto it."""
        self.git("checkout", "-q", "main")
        self.commit(files, "main moved")
        self.track()
        self.git("checkout", "-q", "run")
        self.git("rebase", "-q", "main")


MUL = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
MUL_DOC = (
    "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n"
    '    """The product."""\n    return a * b\n'
)


@pytest.fixture
def repo(tmp_path: Path, git_env: dict[str, str]) -> Repo:
    return Repo(tmp_path / "repo", git_env)


def test_no_change_since_the_pull_request_opened_is_zero(
    repo: Repo, git_env: dict[str, str]
) -> None:
    opened = repo.commit({"src/calc.py": MUL}, "build")
    assert lines_changed(Git(git_env), repo.path, opened=opened, merged=opened) == 0


def test_lines_added_and_removed_after_the_pull_request_opened_are_counted(
    repo: Repo, git_env: dict[str, str]
) -> None:
    opened = repo.commit({"src/calc.py": MUL}, "build")
    merged = repo.commit({"src/calc.py": MUL_DOC, "src/new.py": "A = 1\nB = 2\n"}, "your edit")
    assert lines_changed(Git(git_env), repo.path, opened=opened, merged=merged) == 3


def test_main_moving_under_the_pull_request_isnt_counted(
    repo: Repo, git_env: dict[str, str]
) -> None:
    opened = repo.commit({"src/calc.py": MUL}, "build")
    repo.move_main({"README.md": "one\ntwo\nthree\nfour\n", "src/other.py": "X = 1\n"})
    rebased = repo.git("rev-parse", "HEAD")
    assert lines_changed(Git(git_env), repo.path, opened=opened, merged=rebased) == 0
    merged = repo.commit({"src/calc.py": MUL_DOC}, "your edit")
    assert lines_changed(Git(git_env), repo.path, opened=opened, merged=merged) == 1


def test_a_change_that_cant_be_carried_onto_the_new_main_isnt_measured(
    repo: Repo, git_env: dict[str, str]
) -> None:
    """Main changed the same lines: there is no clean "as opened, on the new main" to compare
    with, so the count is unknown rather than guessed."""
    opened = repo.commit({"src/calc.py": MUL}, "build")
    repo.git("checkout", "-q", "main")
    repo.commit({"src/calc.py": "def add(a, b):\n    return b + a\n\n\nZ = 0\n"}, "main moved")
    repo.track()
    repo.git("checkout", "-q", "-b", "resolved")
    merged = repo.commit({"src/calc.py": MUL + "\n\nZ = 0\n"}, "resolved by hand")
    assert lines_changed(Git(git_env), repo.path, opened=opened, merged=merged) is None
