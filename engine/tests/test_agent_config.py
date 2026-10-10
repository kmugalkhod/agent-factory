"""Tests for factory_engine.agent_config (task 2.7): each agent's CLAUDE_CONFIG_DIR."""

import json
from pathlib import Path, PureWindowsPath
from typing import Any

import pytest

from factory_engine.agent_config import agent_config_dir, path_rule, write_agent_config
from factory_engine.config import Settings, default_settings
from factory_engine.errors import AgentConfigError, DataFolderError
from factory_engine.run import Role

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS = REPO_ROOT / "plugin" / "skills"
ALL_SKILLS = sorted(p.name for p in SKILLS.iterdir() if (p / "SKILL.md").is_file())

COMMANDS = {"test": "uv run pytest", "build": None, "lint": "uv run ruff check .", "smoke": None}
ROLES: tuple[Role, ...] = ("planner", "tester", "builder", "reviewer")


def _settings(**role_changes: dict[str, object]) -> Settings:
    base = default_settings("win32")
    roles = base.roles.model_copy(
        update={
            role: getattr(base.roles, role).model_copy(update=changes)
            for role, changes in role_changes.items()
        }
    )
    commands = base.commands.model_copy(update=COMMANDS)
    return base.model_copy(update={"roles": roles, "commands": commands})


def _write(
    tmp_path: Path,
    role: Role,
    settings: Settings | None = None,
    other_repos: tuple[Path, ...] = (),
) -> Path:
    config_dir = tmp_path / "agents" / "app" / "3-status-json" / role
    write_agent_config(
        config_dir,
        role=role,
        settings=settings or _settings(),
        run_folder=tmp_path / "runs" / "app" / "3-status-json",
        skills_source=SKILLS,
        other_repos=other_repos,
    )
    return config_dir


def _permissions(config_dir: Path) -> dict[str, Any]:
    data = json.loads((config_dir / "settings.json").read_text(encoding="utf-8"))
    return data["permissions"]


# ------------------------------------------------------------------- folder


def test_config_dir_is_outside_the_run_folder(tmp_path: Path) -> None:
    folder = agent_config_dir(tmp_path, "app", 3, "status-json", "builder")
    assert folder == tmp_path / "agents" / "app" / "3-status-json" / "builder"
    assert not folder.is_relative_to(tmp_path / "runs")


def test_config_dir_refuses_unsafe_names(tmp_path: Path) -> None:
    with pytest.raises(DataFolderError, match="repo name"):
        agent_config_dir(tmp_path, "../x", 3, "status-json", "builder")


# -------------------------------------------------------------- path rules


@pytest.mark.parametrize(
    ("path", "rule"),
    [
        (PureWindowsPath(r"C:\Users\me\repos\api"), "//c/Users/me/repos/api"),
        (PureWindowsPath(r"d:\Work\app"), "//d/Work/app"),
    ],
)
def test_windows_paths_become_absolute_rules(path: PureWindowsPath, rule: str) -> None:
    assert path_rule(path) == rule


def test_unc_paths_are_refused() -> None:
    with pytest.raises(AgentConfigError) as exc:
        path_rule(PureWindowsPath(r"\\server\share\repo"))
    assert "server" in str(exc.value)


# ------------------------------------------------------------------- deny


@pytest.mark.parametrize("role", ROLES)
def test_deny_rules_cover_secrets_for_reads_and_edits(tmp_path: Path, role: Role) -> None:  # AC1
    deny = _permissions(_write(tmp_path, role))["deny"]
    for target in ("~/.ssh/**", "~/.aws/**", "~/.claude/**", "//**/.env*"):
        assert f"Read({target})" in deny
        assert f"Edit({target})" in deny


def test_deny_rules_cover_other_repos(tmp_path: Path) -> None:  # AC1
    others = (tmp_path / "repos" / "api", tmp_path / "worktrees" / "app-2-other")
    deny = _permissions(_write(tmp_path, "builder", other_repos=others))["deny"]
    for other in others:
        rule = path_rule(other.resolve())
        assert f"Read({rule}/**)" in deny
        assert f"Edit({rule}/**)" in deny


@pytest.mark.parametrize("role", ROLES)
def test_outside_the_worktree_is_refused_not_prompted(tmp_path: Path, role: Role) -> None:  # AC1
    """Reads and edits outside the working folders are never auto-approved; dontAsk refuses
    them instead of asking. The run folder is the one extra working folder."""
    permissions = _permissions(_write(tmp_path, role))
    assert permissions["defaultMode"] == "dontAsk"
    assert permissions["additionalDirectories"] == [
        str((tmp_path / "runs" / "app" / "3-status-json").resolve())
    ]


def test_a_role_with_no_shell_has_the_shell_tools_denied(tmp_path: Path) -> None:
    deny = _permissions(_write(tmp_path, "reviewer"))["deny"]
    assert "Bash" in deny
    assert "PowerShell" in deny


@pytest.mark.parametrize("role", ["planner", "tester", "builder"])
def test_roles_with_a_shell_keep_the_shell_tools(tmp_path: Path, role: Role) -> None:
    deny = _permissions(_write(tmp_path, role))["deny"]
    assert "Bash" not in deny


# ------------------------------------------------------------------ allow


@pytest.mark.parametrize(
    ("role", "commands"),
    [
        ("planner", []),
        ("tester", ["uv run pytest", "uv run ruff check ."]),
        ("builder", ["uv run pytest", "uv run ruff check ."]),  # build isn't configured
        ("reviewer", []),
    ],
)
def test_allow_rules_equal_the_role_shell_allowlist(  # AC2
    tmp_path: Path, role: Role, commands: list[str]
) -> None:
    allow = _permissions(_write(tmp_path, role))["allow"]
    assert allow == [rule for cmd in commands for rule in (f"Bash({cmd})", f"Bash({cmd} *)")]


def test_allow_rules_follow_a_changed_allowlist(tmp_path: Path) -> None:  # AC2
    settings = _settings(builder={"shell_allowlist": ["lint"]})
    allow = _permissions(_write(tmp_path, "builder", settings))["allow"]
    assert allow == ["Bash(uv run ruff check .)", "Bash(uv run ruff check . *)"]


# ----------------------------------------------------------------- skills


def _skills(config_dir: Path) -> list[str]:
    return sorted(p.name for p in (config_dir / "skills").iterdir())


@pytest.mark.parametrize("role", ROLES)
def test_no_skill_list_means_every_factory_skill(tmp_path: Path, role: Role) -> None:  # AC3
    assert _skills(_write(tmp_path, role)) == ALL_SKILLS


def test_a_skill_list_copies_only_those_skills(tmp_path: Path) -> None:  # AC3
    settings = _settings(planner={"skills": ["write-plan", "acceptance-criteria"]})
    config_dir = _write(tmp_path, "planner", settings)
    assert _skills(config_dir) == ["acceptance-criteria", "write-plan"]
    for name in ("acceptance-criteria", "write-plan"):
        for source in (SKILLS / name).iterdir():
            assert (config_dir / "skills" / name / source.name).read_bytes() == source.read_bytes()


def test_an_empty_skill_list_copies_no_skills(tmp_path: Path) -> None:  # AC3
    assert _skills(_write(tmp_path, "reviewer", _settings(reviewer={"skills": []}))) == []


def test_an_unknown_skill_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AgentConfigError) as exc:
        _write(tmp_path, "planner", _settings(planner={"skills": ["write-plan", "no-such"]}))
    assert "no-such" in str(exc.value)


# ----------------------------------------------------- nothing personal


def test_nothing_from_the_users_claude_folder_is_copied(  # AC4
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    personal = home / ".claude"
    (personal / "skills" / "my-skill").mkdir(parents=True)
    (personal / "skills" / "my-skill" / "SKILL.md").write_text("personal", encoding="utf-8")
    (personal / "settings.json").write_text('{"model": "personal"}', encoding="utf-8")
    (personal / "history.jsonl").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))

    config_dir = _write(tmp_path, "builder")
    files = sorted(
        p.relative_to(config_dir).as_posix() for p in config_dir.rglob("*") if p.is_file()
    )
    expected_skills = sorted(
        f"skills/{s.parent.name}/{s.name}" for s in SKILLS.glob("*/*") if s.is_file()
    )
    assert files == sorted(["settings.json", *expected_skills])
    assert "personal" not in (config_dir / "settings.json").read_text(encoding="utf-8")


def test_settings_json_holds_only_permissions(tmp_path: Path) -> None:
    data = json.loads((_write(tmp_path, "builder") / "settings.json").read_text(encoding="utf-8"))
    assert set(data) == {"permissions"}


# ---------------------------------------------------------------- rewrite


def test_rewriting_keeps_sessions_and_resyncs_skills(tmp_path: Path) -> None:
    config_dir = _write(tmp_path, "planner")
    session = config_dir / "projects" / "worktree" / "session-1.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text("{}", encoding="utf-8")
    (config_dir / "settings.json").write_text("{}", encoding="utf-8")

    _write(tmp_path, "planner", _settings(planner={"skills": ["write-plan"]}))
    assert session.read_text(encoding="utf-8") == "{}"  # kept for resume
    assert _skills(config_dir) == ["write-plan"]
    assert "permissions" in json.loads((config_dir / "settings.json").read_text(encoding="utf-8"))
