"""Tests for factory_engine.readiness (task 2.25): the readiness bar checked before a feature
run. Fixture repos are a temporary checkout whose `origin` says GitHub but is rewritten (git's
`insteadOf`, set through the environment) to a bare repo on disk; `gh` is a fake on PATH. No
network and no real model."""

import logging
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from factory_engine.config import CriticalFlow, Settings, default_settings, merge_settings
from factory_engine.config import parse_overrides as _parse_overrides
from factory_engine.errors import NotReadyError
from factory_engine.git_ops import Git, run_command
from factory_engine.github import GitHub
from factory_engine.readiness import (
    STALE_AFTER,
    Readiness,
    _writable_and_retry,  # pyright: ignore[reportPrivateUsage] -- its logging is the behaviour
    check_readiness,
    flow_problems,
    github_remote,
)

GITHUB_URL = "https://github.com/acme/pilot.git"
FILES = {
    "CLAUDE.md": "# Pilot\n\nBuild, test and lint with the commands in factory.yaml.\n",
    "factory.yaml": "commands:\n  test: python scripts/ok.py\n",
    "src/app.py": "def main():\n    return 0\n",
    "tests/test_login.py": "def test_login():\n    assert True\n",
    "scripts/ok.py": "print('ok')\n",
    "scripts/fail.py": "import sys\nprint('boom: it broke')\nsys.exit(3)\n",
    "scripts/slow.py": "import time\ntime.sleep(120)\n",
}
OK = "python scripts/ok.py"
READY: dict[str, Any] = {
    "commands": {"build": OK, "test": OK, "lint": OK, "smoke": OK},
    "critical_flows": [{"name": "login", "tests": ["tests/test_login.py"]}],
}


def settings(**changes: Any) -> Settings:
    """The ready repo's settings, with top-level keys or `commands` entries changed."""
    data: dict[str, Any] = {
        "commands": dict(READY["commands"]),
        "critical_flows": list(READY["critical_flows"]),
    }
    for key, value in changes.items():
        if key in data["commands"]:
            data["commands"][key] = value
        else:
            data[key] = value
    return merge_settings(default_settings("linux"), _parse_overrides(data, "test settings"))


def clean_env(gitconfig: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=str(gitconfig),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Factory Test",
        GIT_AUTHOR_EMAIL="factory@example.com",
        GIT_COMMITTER_NAME="Factory Test",
        GIT_COMMITTER_EMAIL="factory@example.com",
    )
    return env


def sh(cwd: Path, *args: str, env: Mapping[str, str]) -> str:
    """Run git directly, not through the code under test. Returns stdout, stripped."""
    done = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=dict(env),
        check=True,
        capture_output=True,
        timeout=60,
        encoding="utf-8",
        errors="replace",
    )
    return done.stdout.strip()


def fake_gh(folder: Path, *, logged_in: bool) -> Path:
    """A `gh` that answers `auth status` as logged in (exit 0) or not (exit 1)."""
    folder.mkdir(parents=True)
    message = "You are not logged into any GitHub hosts. To log in, run: gh auth login"
    if sys.platform == "win32":
        body = "@exit /b 0\r\n" if logged_in else f"@echo {message} 1>&2\r\n@exit /b 1\r\n"
        (folder / "gh.cmd").write_text(body, encoding="utf-8")
    else:
        body = "#!/bin/sh\nexit 0\n" if logged_in else f"#!/bin/sh\necho '{message}' >&2\nexit 1\n"
        (folder / "gh").write_text(body, encoding="utf-8")
        (folder / "gh").chmod(0o755)
    return folder


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A bare `remote.git` with a ready `main`, and its clone `pilot` whose origin says GitHub."""
    root = tmp_path_factory.mktemp("template")
    (root / "gitconfig").write_text("", encoding="utf-8")
    env = clean_env(root / "gitconfig")
    remote = root / "remote.git"
    sh(root, "init", "--bare", "-b", "main", str(remote), env=env)
    repo = root / "pilot"
    sh(root, "clone", str(remote), str(repo), env=env)
    for name, text in FILES.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    sh(repo, "add", "-A", env=env)
    sh(repo, "commit", "-m", "ready", env=env)
    sh(repo, "push", "origin", "main", env=env)
    sh(repo, "remote", "set-url", "origin", GITHUB_URL, env=env)
    return root


@dataclass
class Fixture:
    root: Path
    env: dict[str, str]
    calls: list[tuple[list[str], Path, dict[str, str]]] = field(default_factory=list)

    @property
    def repo(self) -> Path:
        return self.root / "pilot"

    @property
    def remote(self) -> Path:
        return self.root / "remote.git"

    @property
    def scratch(self) -> Path:
        return self.root / "data" / "readiness"

    def git(self, *args: str) -> str:
        return sh(self.repo, *args, env=self.env)

    def push(self, message: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-m", message)
        self.git("push", "origin", "main")

    def logged_out(self) -> None:
        bad = fake_gh(self.root / "gh-bad", logged_in=False)
        self.env["PATH"] = f"{bad}{os.pathsep}{self.env['PATH']}"

    def runner(
        self, argv: Sequence[str], cwd: Path, env: Mapping[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), cwd, dict(env)))
        return run_command(argv, cwd, env, timeout)

    def check(self, chosen: Settings | None = None, **kw: Any) -> Readiness:
        kw.setdefault("platform", "linux")
        # Process starts are slow on some Windows machines (antivirus); the timeout's own
        # behaviour isn't what these tests check.
        kw.setdefault("remote_timeout", 180.0)
        return check_readiness(
            self.repo,
            chosen if chosen is not None else settings(),
            git=Git(self.env),
            github=GitHub(self.env),
            parent_env=self.env,
            scratch=self.scratch,
            runner=self.runner,
            **kw,
        )


@pytest.fixture
def fx(template: Path, tmp_path: Path) -> Fixture:
    root = tmp_path / "fx"
    shutil.copytree(template, root, symlinks=True)
    env = clean_env(root / "gitconfig")
    # `origin` says GitHub; git reads the bare repo on disk instead.
    env.update(
        GIT_CONFIG_COUNT="1",
        GIT_CONFIG_KEY_0=f"url.{(root / 'remote.git').as_posix()}.insteadOf",
        GIT_CONFIG_VALUE_0=GITHUB_URL,
    )
    ok = fake_gh(root / "gh-ok", logged_in=True)
    env["PATH"] = f"{ok}{os.pathsep}{env.get('PATH', '')}"
    env["CLAUDE_CODE_OAUTH_TOKEN"] = "secret-token"
    return Fixture(root=root, env=env)


def conditions(result: Readiness) -> list[str]:
    return [problem.condition for problem in result.problems]


# ------------------------------------------------------------------ pure rules


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/acme/pilot.git", True),
        ("https://github.com/acme/pilot", True),
        ("https://GitHub.com/acme/pilot/", True),
        ("https://user:token@github.com/acme/pilot.git", True),
        ("ssh://git@github.com/acme/pilot.git", True),
        ("ssh://git@ssh.github.com:443/acme/pilot.git", True),
        ("git@github.com:acme/pilot.git", True),
        ("https://gitlab.com/acme/pilot.git", False),
        ("https://github.com.evil.example/acme/pilot.git", False),
        ("https://evil.example/github.com/acme/pilot.git", False),
        ("https://github.com/acme", False),
        ("git@gitlab.com:acme/pilot.git", False),
        ("D:/repos/pilot.git", False),
        ("/srv/git/pilot.git", False),
        ("", False),
    ],
)
def test_github_remote(url: str, expected: bool) -> None:
    assert github_remote(url) is expected


FLOW_FILES = ("tests/test_login.py", "tests/flows/test_pay.py", "src/app.py")


@pytest.mark.parametrize(
    "tests,ready",
    [
        (["tests/test_login.py"], True),
        (["tests/missing.py", "tests/test_login.py"], True),
        (["tests/test_login.py::test_login"], True),
        (["./tests/test_login.py"], True),
        (["tests\\test_login.py"], True),
        (["tests/flows"], True),
        (["tests/flows/"], True),
        (["tests/missing.py"], False),
        ([], False),
        (["tests/test_login"], False),
        (["../pilot/tests/test_login.py"], False),
        (["/tests/test_login.py"], False),
        (["C:/tests/test_login.py"], False),
        (["tests/../tests/test_login.py"], False),
        (["Tests/test_login.py"], False),
    ],
)
def test_a_critical_flow_needs_one_existing_test(tests: list[str], ready: bool) -> None:
    problems = flow_problems([CriticalFlow(name="login", tests=tests)], FLOW_FILES)

    assert (problems == []) is ready
    if not ready:
        [problem] = problems
        assert problem.condition == "critical_flow"
        assert "`login`" in problem.message


def test_each_critical_flow_is_judged_on_its_own() -> None:
    flows = [
        CriticalFlow(name="login", tests=["tests/test_login.py"]),
        CriticalFlow(name="pay", tests=[]),
        CriticalFlow(name="refund", tests=["tests/test_refund.py"]),
    ]

    problems = flow_problems(flows, FLOW_FILES)

    assert [p.condition for p in problems] == ["critical_flow", "critical_flow"]
    assert "`pay`" in problems[0].message and "lists no tests" in problems[0].message
    assert "`refund`" in problems[1].message and "tests/test_refund.py" in problems[1].message


# ------------------------------------------------------------------ a ready repo


def test_a_ready_repo_passes(fx: Fixture) -> None:
    chosen = settings(test="python -m pytest -q tests")

    result = fx.check(chosen)

    assert result.problems == ()
    assert result.ready
    result.require(fx.repo)  # no error
    # The four commands ran in a checkout of origin/main outside the repo, with a clean
    # environment, and the checkout is gone afterwards.
    assert len(fx.calls) == 4
    for _argv, cwd, env in fx.calls:
        assert not cwd.resolve().is_relative_to(fx.repo.resolve())
        assert cwd.resolve().is_relative_to(fx.scratch.resolve())
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert not any(fx.scratch.iterdir())
    assert fx.git("worktree", "list", "--porcelain").count("worktree ") == 1


def test_files_only_in_the_checkout_dont_count(fx: Fixture) -> None:
    fx.git("rm", "-q", "CLAUDE.md")
    fx.push("drop CLAUDE.md")
    (fx.repo / "CLAUDE.md").write_text("# Pilot\n", encoding="utf-8")  # untracked

    result = fx.check()

    assert conditions(result) == ["claude_md"]
    assert "origin/main" in result.problems[0].message


@pytest.mark.parametrize("name", ["CLAUDE.md", "factory.yaml"])
def test_a_missing_file_is_named(fx: Fixture, name: str) -> None:
    fx.git("rm", "-q", name)
    fx.push(f"drop {name}")

    result = fx.check()

    assert conditions(result) == ["claude_md" if name == "CLAUDE.md" else "factory_yaml"]
    assert name in result.problems[0].message


# ------------------------------------------------------------------ commands


def test_a_missing_test_command_is_named(fx: Fixture) -> None:
    result = fx.check(settings(test=None))

    assert conditions(result) == ["test"]
    assert "`commands.test`" in result.problems[0].message
    assert "for example" in result.problems[0].message
    assert len(fx.calls) == 3  # the other commands still ran


def test_a_repo_with_only_a_test_command_is_ready(fx: Fixture) -> None:
    # Build, lint and smoke are optional (agreed with Kunal, Oct 17, 2026).
    result = fx.check(settings(build=None, lint=None, smoke=None))

    assert result.ready
    assert len(fx.calls) == 1


@pytest.mark.parametrize("name", ["build", "test", "lint", "smoke"])
def test_a_failing_command_is_named_with_its_output(fx: Fixture, name: str) -> None:
    result = fx.check(settings(**{name: "python scripts/fail.py"}))

    assert conditions(result) == [name]
    message = result.problems[0].message
    assert "`python scripts/fail.py`" in message
    assert "exit 3" in message
    assert "boom: it broke" in message


def test_a_command_that_needs_a_shell_is_refused_without_running(fx: Fixture) -> None:
    result = fx.check(settings(lint="python scripts/ok.py && python scripts/ok.py"))

    assert conditions(result) == ["lint"]
    assert "`&&`" in result.problems[0].message
    assert len(fx.calls) == 3


def test_a_command_past_its_timeout_is_stopped(fx: Fixture) -> None:
    result = fx.check(settings(smoke="python scripts/slow.py"), command_timeout=15)

    assert conditions(result) == ["smoke"]
    assert "longer than 15s" in result.problems[0].message
    assert not any(fx.scratch.iterdir())


def test_a_critical_flow_without_an_existing_test_is_named(fx: Fixture) -> None:
    chosen = settings(critical_flows=[{"name": "checkout", "tests": ["tests/test_checkout.py"]}])

    result = fx.check(chosen)

    assert conditions(result) == ["critical_flow"]
    assert "`checkout`" in result.problems[0].message


# ------------------------------------------------------------------ isolation


def test_sandbox_required_is_refused_on_native_windows(fx: Fixture) -> None:
    result = fx.check(settings(sandbox_required=True), platform="win32")

    assert conditions(result) == ["sandbox"]
    message = result.problems[0].message
    assert "sandbox_required" in message
    assert "WSL" in message and "after version 1" in message
    assert fx.calls == []  # the repo's code never ran unconfined


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_sandbox_required_is_refused_until_a_sandbox_runner_exists(
    fx: Fixture, platform: str
) -> None:
    result = fx.check(settings(sandbox_required=True), platform=platform)

    assert conditions(result) == ["sandbox"]
    assert "sandbox runner isn't built" in result.problems[0].message
    assert fx.calls == []  # fail closed: no command runs unconfined on any platform


# ------------------------------------------------------------------ clean-up


def test_a_stale_checkout_left_by_an_earlier_check_is_cleared(fx: Fixture) -> None:
    fx.scratch.mkdir(parents=True)
    stale, fresh = fx.scratch / "check-stale", fx.scratch / "check-fresh"
    for folder in (stale, fresh):
        (folder / "tree").mkdir(parents=True)
        (folder / "tree" / "left.txt").write_text("x", encoding="utf-8")
    now = time.time()
    os.utime(stale, (now - STALE_AFTER - 60, now - STALE_AFTER - 60))

    result = fx.check(clock=lambda: now)

    assert result.ready
    assert not stale.exists()
    assert fresh.exists()  # may belong to a check still running elsewhere


def test_a_checkout_that_cant_be_deleted_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def busy(path: str) -> None:
        raise PermissionError(f"{path} is in use")

    with caplog.at_level(logging.WARNING, logger="factory_engine.readiness"):
        _writable_and_retry(busy, str(tmp_path), PermissionError("in use"))

    assert str(tmp_path) in caplog.text


# ------------------------------------------------------------------ GitHub


def test_a_repo_without_origin_is_refused(fx: Fixture) -> None:
    fx.git("remote", "remove", "origin")

    result = fx.check()

    assert conditions(result) == ["origin", "unchecked"]
    assert "git remote add origin" in result.problems[0].message
    assert fx.calls == []


def test_an_origin_that_isnt_github_is_refused(fx: Fixture) -> None:
    fx.git("remote", "set-url", "origin", str(fx.remote))

    result = fx.check()

    assert conditions(result) == ["origin"]  # reachable, so the rest is still checked
    assert "github.com" in result.problems[0].message
    assert len(fx.calls) == 4


def test_an_unreachable_origin_suggests_gh_auth_setup_git(fx: Fixture) -> None:
    fx.remote.rename(fx.root / "gone.git")

    result = fx.check()

    assert conditions(result) == ["origin_reachable", "unchecked"]
    message = result.problems[0].message
    assert "gh auth setup-git" in message
    assert "git ls-remote origin" in message
    assert fx.calls == []


def test_an_origin_that_doesnt_answer_in_time_is_stopped(fx: Fixture) -> None:
    result = fx.check(remote_timeout=0.001)

    assert conditions(result)[:1] == ["origin_reachable"]
    message = result.problems[0].message
    assert "didn't answer within 0.001s" in message
    assert "login window" in message and "gh auth setup-git" in message
    assert fx.calls == []


def test_a_remote_url_password_is_never_shown(fx: Fixture) -> None:
    fx.git("remote", "set-url", "origin", "https://kunal:hunter2secret@github.com/acme/none.git")
    fx.env["GIT_CONFIG_VALUE_0"] = "https://kunal:hunter2secret@github.com/acme/none.git"
    fx.remote.rename(fx.root / "gone.git")

    result = fx.check()

    assert "origin_reachable" in conditions(result)
    assert all("hunter2secret" not in problem.message for problem in result.problems)


def test_origin_without_main_is_refused(fx: Fixture) -> None:
    sh(fx.remote, "branch", "trunk", "main", env=fx.env)
    sh(fx.remote, "symbolic-ref", "HEAD", "refs/heads/trunk", env=fx.env)
    sh(fx.remote, "update-ref", "-d", "refs/heads/main", env=fx.env)

    result = fx.check()

    assert conditions(result) == ["origin_main", "unchecked"]
    assert "git push -u origin main" in result.problems[0].message


def test_gh_logged_out_is_refused(fx: Fixture) -> None:
    fx.logged_out()

    result = fx.check()

    assert conditions(result) == ["gh_auth"]
    assert "gh auth login" in result.problems[0].message


# ------------------------------------------------------------------ every problem at once


def test_every_failing_condition_is_reported_together(fx: Fixture) -> None:
    fx.git("rm", "-q", "CLAUDE.md")
    fx.push("drop CLAUDE.md")
    fx.logged_out()

    result = fx.check(settings(test=None, smoke="python scripts/fail.py"))

    assert conditions(result) == ["gh_auth", "claude_md", "test", "smoke"]
    with pytest.raises(NotReadyError) as caught:
        result.require(fx.repo)
    text = str(caught.value)
    assert str(fx.repo) in text
    for number, problem in enumerate(result.problems, start=1):
        assert f"{number}. {problem.message.splitlines()[0]}" in text
