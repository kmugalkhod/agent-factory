"""Each agent's own Claude Code config folder (`CLAUDE_CONFIG_DIR`).

The folder holds a `settings.json` with the role's permission rules and a copy of the factory
skills the role may use. Nothing comes from your own `~/.claude`: no personal settings, skills
or history. Claude Code keeps the agent's session transcripts here too, so the folder lives
outside the run folder, where other roles can't read them.

How the rules keep an agent in its worktree:
- `defaultMode: dontAsk` refuses any call that isn't approved instead of asking. Claude Code
  approves reads and edits on its own only inside the working folders: the worktree, plus the
  run folder added through `additionalDirectories`. So everything outside them is refused.
- Deny rules, which beat every allow rule, name the sensitive places: `~/.ssh`, `~/.aws`, your
  own `~/.claude`, `.env*` files anywhere, and other repos and runs the caller lists.
- Allow rules are exactly the role's shell allowlist; a role with no shell has the shell tools
  denied outright. Write paths are enforced by the runner and the path hook, not here.
"""

import os
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path, PurePath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from factory_engine.config import RoleSettings, Settings
from factory_engine.errors import AgentConfigError
from factory_engine.paths import run_dir
from factory_engine.run import Role

AGENTS_DIR = "agents"
SETTINGS_FILE = "settings.json"
SKILLS_DIR = "skills"

SECRET_PATTERNS = ("~/.ssh/**", "~/.aws/**", "~/.claude/**", "//**/.env*")
SHELL_TOOLS = ("Bash", "PowerShell")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Permissions(_Model):
    """The `permissions` block of an agent's `settings.json`, in Claude Code's field names."""

    default_mode: Literal["dontAsk"] = Field(alias="defaultMode")
    additional_directories: list[str] = Field(alias="additionalDirectories")
    allow: list[str]
    deny: list[str]


class AgentSettings(_Model):
    """An agent's whole `settings.json`: permission rules and nothing else."""

    permissions: Permissions


def agent_config_dir(data: Path, repo: str, run_id: int, slug: str, role: Role) -> Path:
    """`<data>/agents/<repo>/<id>-<slug>/<role>/`, next to the run folder rather than inside it."""
    run_dir(data, repo, run_id, slug)  # refuse unsafe repo names and slugs
    return data / AGENTS_DIR / repo / f"{run_id}-{slug}" / role


def path_rule(path: PurePath) -> str:
    """An absolute path in Claude Code's rule form: `C:\\Users\\me` becomes `//c/Users/me`."""
    windows = PureWindowsPath(str(path))
    drive = windows.drive
    if not drive or drive.startswith("\\\\") or not drive.endswith(":"):
        raise AgentConfigError(
            f"{path}: can't write a permission rule for this path; use a path on a drive "
            "letter, not a network (UNC) path."
        )
    rest = "/".join(windows.parts[1:])
    return f"//{drive[0].lower()}/{rest}" if rest else f"//{drive[0].lower()}"


def write_agent_config(
    config_dir: Path,
    *,
    role: Role,
    settings: Settings,
    run_folder: Path,
    skills_source: Path,
    other_repos: Sequence[Path] = (),
) -> Path:
    """Create or refresh one agent's config folder. Returns the `settings.json` path.

    Refreshing rewrites `settings.json` and brings the skills in line with the role's list;
    anything else already in the folder, such as saved sessions for resume, is kept.
    """
    role_settings: RoleSettings = getattr(settings.roles, role)
    agent_settings = AgentSettings(
        permissions=Permissions(
            defaultMode="dontAsk",
            additionalDirectories=[str(run_folder.resolve())],
            allow=_allow_rules(role_settings, settings),
            deny=_deny_rules(role_settings, other_repos),
        )
    )
    try:
        skills = _wanted_skills(role_settings, skills_source)
    except OSError as err:
        raise AgentConfigError(
            f"{skills_source}: can't read the factory skills folder ({err}). "
            "Check that the folder exists and is readable."
        ) from err
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        _sync_skills(config_dir, skills, skills_source)
        return _write_settings(config_dir / SETTINGS_FILE, agent_settings)
    except OSError as err:
        raise AgentConfigError(f"{config_dir}: can't write the agent config ({err}).") from err


def _allow_rules(role: RoleSettings, settings: Settings) -> list[str]:
    rules: list[str] = []
    for name in role.shell_allowlist:
        command: str | None = getattr(settings.commands, name)
        if command:  # a command that isn't configured adds no rule
            rules += [f"Bash({command})", f"Bash({command} *)"]
    return rules


def _deny_rules(role: RoleSettings, other_repos: Sequence[Path]) -> list[str]:
    targets = [*SECRET_PATTERNS, *(f"{path_rule(p.resolve())}/**" for p in other_repos)]
    rules = [f"{tool}({target})" for target in targets for tool in ("Read", "Edit")]
    if not role.shell_read_only and not role.shell_allowlist:
        rules += list(SHELL_TOOLS)  # no shell at all: remove the tools
    return rules


def _wanted_skills(role: RoleSettings, source: Path) -> list[str]:
    available = sorted(p.name for p in source.iterdir() if (p / "SKILL.md").is_file())
    if role.skills is None:
        return available
    unknown = sorted(set(role.skills) - set(available))
    if unknown:
        raise AgentConfigError(
            f"unknown skills {unknown} in the role's skill list; available in {source}: "
            f"{', '.join(available)}."
        )
    return sorted(set(role.skills))


def _sync_skills(config_dir: Path, wanted: list[str], source: Path) -> None:
    """Copy the wanted skills into a staging folder, then swap it in for the old `skills/`.

    A failed copy leaves the old skills untouched.
    """
    target = config_dir / SKILLS_DIR
    staging = Path(tempfile.mkdtemp(prefix=f"{SKILLS_DIR}.", suffix=".tmp", dir=config_dir))
    try:
        for name in wanted:
            shutil.copytree(source / name, staging / name)
        old = None
        if target.exists() or target.is_symlink():
            old = Path(tempfile.mkdtemp(prefix=f"{SKILLS_DIR}.", suffix=".old", dir=config_dir))
            old.rmdir()
            target.replace(old)
        staging.replace(target)
    except BaseException:
        _remove(staging)
        raise
    if old is not None:
        _remove(old)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _write_settings(path: Path, data: AgentSettings) -> Path:
    fd, temp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(data.model_dump_json(by_alias=True, indent=2) + "\n")
        temp.replace(path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return path
