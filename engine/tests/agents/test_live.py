"""Live test for factory_engine.agents.runner (task 2.12): a real agent does a one-line task.

Skipped by default; run with `uv run pytest -m live`. Needs CLAUDE_CODE_OAUTH_TOKEN.
"""

import asyncio
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from factory_engine.agent_config import write_agent_config
from factory_engine.agents.runner import ClaudeAgentRunner, SessionRecorder, build_spec
from factory_engine.config import default_settings
from factory_engine.events import EventLog
from factory_engine.providers import PROFILES, TOKEN_VAR
from factory_engine.registry import open_registry
from factory_engine.run import load_run, new_run, save_run
from factory_engine.safety.hook import HookPolicy, make_safety_hook

REPO_ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.live


def _clock() -> datetime:
    return datetime.now(UTC)


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, timeout=60, encoding="utf-8", errors="replace"
    )


@pytest.mark.skipif(not os.environ.get(TOKEN_VAR), reason=f"needs {TOKEN_VAR}")
def test_agent_completes_a_one_line_task_in_a_temporary_repo(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    (worktree / "src").mkdir(parents=True)
    _git(["init", "-q"], worktree)
    run_folder = tmp_path / "run"
    run_folder.mkdir()
    config_dir = tmp_path / "agent"
    settings = default_settings("win32")
    write_agent_config(
        config_dir,
        role="builder",
        settings=settings,
        run_folder=run_folder,
        skills_source=REPO_ROOT / "plugin" / "skills",
    )
    run = new_run(
        run_id=1,
        slug="hello",
        repo="live",
        kind="feature",
        provider="subscription",
        isolation="worktree",
        now=_clock(),
    )
    save_run(run, run_folder)
    events = EventLog(run_folder, repo="live", run_id=1, clock=_clock)
    policy = HookPolicy.build("builder", settings, worktree=worktree, run_folder=run_folder)
    spec = build_spec(
        role="builder",
        attempt=1,
        prompt="Create the file src/hello.txt containing exactly the line: hello. Nothing else.",
        run=run,
        settings=settings,
        profile=PROFILES["subscription"],
        parent_env=os.environ,
        worktree=worktree,
        run_folder=run_folder,
        config_dir=config_dir,
        prompts_dir=REPO_ROOT / "prompts",
        hook=make_safety_hook(policy, events),
        resume=False,
    )
    with open_registry(tmp_path / "registry.db") as registry:
        registry.upsert_run(run)
        recorder = SessionRecorder(
            run,
            run_folder=run_folder,
            events=events,
            registry=registry,
            role="builder",
            attempt=1,
            clock=_clock,
        )
        outcome = asyncio.run(ClaudeAgentRunner(os.environ).run(spec, recorder))

    assert outcome.outcome == "completed"
    hello = worktree / "src" / "hello.txt"
    assert hello.read_text(encoding="utf-8").strip() == "hello"
    saved = load_run(run_folder)
    assert saved.session_ids["builder"] == outcome.session_id
    assert saved.metrics.tokens["builder"] > 0
