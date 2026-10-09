"""Settings models, defaults, merge (run > repo > global) and load/dump of config files."""

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from factory_engine.errors import ConfigError

LogicalModel = Literal["opus", "sonnet", "haiku"]
Effort = Literal["low", "normal", "high"]
CommandName = Literal["test", "build", "lint", "smoke"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RoleSettings(_Model):
    model: LogicalModel
    effort: Effort
    write_paths: list[str]
    deny_paths: list[str]
    shell_read_only: bool
    shell_allowlist: list[CommandName]
    skills: list[str] | None


class Roles(_Model):
    planner: RoleSettings
    tester: RoleSettings
    builder: RoleSettings
    reviewer: RoleSettings


class Commands(_Model):
    test: str | None
    build: str | None
    lint: str | None
    smoke: str | None


class Caps(_Model):
    planner_attempts: int
    tester_attempts: int
    builder_attempts: int
    max_turns: int
    max_minutes: int
    stall_minutes: int


class CriticalFlow(_Model):
    name: str
    tests: list[str]


class RolePatch(_Model):
    model: LogicalModel | None = None
    effort: Effort | None = None
    write_paths: list[str] | None = None
    deny_paths: list[str] | None = None
    shell_read_only: bool | None = None
    shell_allowlist: list[CommandName] | None = None
    skills: list[str] | None = None


class RolesPatch(_Model):
    planner: RolePatch | None = None
    tester: RolePatch | None = None
    builder: RolePatch | None = None
    reviewer: RolePatch | None = None


class CommandsPatch(_Model):
    test: str | None = None
    build: str | None = None
    lint: str | None = None
    smoke: str | None = None


class CapsPatch(_Model):
    planner_attempts: int | None = None
    tester_attempts: int | None = None
    builder_attempts: int | None = None
    max_turns: int | None = None
    max_minutes: int | None = None
    stall_minutes: int | None = None


class Bootstrap(_Model):
    builder: RolePatch


class Settings(_Model):
    roles: Roles
    commands: Commands
    caps: Caps
    provider: str
    allowed_providers: list[str] | None
    isolation: Literal["worktree", "sandbox"]
    sandbox_required: bool
    critical_flows: list[CriticalFlow]
    bootstrap: Bootstrap


class SettingsPatch(_Model):
    roles: RolesPatch | None = None
    commands: CommandsPatch | None = None
    caps: CapsPatch | None = None
    provider: str | None = None
    allowed_providers: list[str] | None = None
    isolation: Literal["worktree", "sandbox"] | None = None
    sandbox_required: bool | None = None
    critical_flows: list[CriticalFlow] | None = None


class GlobalPatch(SettingsPatch):
    bootstrap: Bootstrap | None = None


def default_settings(platform: str) -> Settings:
    """Built-in defaults. `platform` is `sys.platform`; passed in so tests can vary it."""
    return Settings.model_validate(
        {
            "roles": {
                "planner": _role("opus", "high", ["plan.md"], [], True, []),
                "tester": _role("sonnet", "normal", ["tests/**"], ["src/**"], False, ["test"]),
                "builder": _role(
                    "sonnet",
                    "normal",
                    ["src/**"],
                    ["tests/**", "plan.md"],
                    False,
                    ["test", "build", "lint"],
                ),
                "reviewer": _role("opus", "high", ["review.md"], [], False, []),
            },
            "commands": {"test": None, "build": None, "lint": None, "smoke": None},
            "caps": {
                "planner_attempts": 3,
                "tester_attempts": 3,
                "builder_attempts": 3,
                "max_turns": 200,
                "max_minutes": 120,
                "stall_minutes": 10,
            },
            "provider": "subscription",
            "allowed_providers": None,
            "isolation": "worktree" if platform == "win32" else "sandbox",
            "sandbox_required": False,
            "critical_flows": [],
            "bootstrap": {"builder": {"write_paths": ["**"], "deny_paths": ["plan.md"]}},
        }
    )


def _role(
    model: str,
    effort: str,
    write_paths: list[str],
    deny_paths: list[str],
    shell_read_only: bool,
    shell_allowlist: list[str],
) -> dict[str, Any]:
    return {
        "model": model,
        "effort": effort,
        "write_paths": write_paths,
        "deny_paths": deny_paths,
        "shell_read_only": shell_read_only,
        "shell_allowlist": shell_allowlist,
        "skills": None,
    }


def _deep_merge(base: dict[str, Any], top: dict[str, Any]) -> dict[str, Any]:
    """Mappings merge per key; lists, scalars and nulls in `top` replace."""
    out = dict(base)
    for key, value in top.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def merge_settings(
    global_settings: Settings,
    repo: SettingsPatch | None = None,
    run: SettingsPatch | None = None,
    *,
    bootstrap: bool = False,
) -> Settings:
    """Merge run > repo > global. With `bootstrap`, the builder patch sits above global."""
    merged = global_settings.model_dump()
    merged["bootstrap"] = global_settings.bootstrap.model_dump(exclude_unset=True)
    if bootstrap:
        patch = global_settings.bootstrap.builder.model_dump(exclude_unset=True)
        merged = _deep_merge(merged, {"roles": {"builder": patch}})
    for layer in (repo, run):
        if layer is not None:
            merged = _deep_merge(merged, layer.model_dump(exclude_unset=True))
    try:
        return Settings.model_validate(merged)
    except ValidationError as err:
        raise ConfigError(_format_errors("merged settings", err)) from err


def _format_errors(source: str, err: ValidationError) -> str:
    lines = []
    for item in err.errors():
        key = ".".join(str(part) for part in item["loc"]) or "<root>"
        lines.append(f"{source}: {key}: {item['msg']}")
    return "\n".join(lines)


def _validate[T: BaseModel](cls: type[T], data: object, source: str) -> T:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(
            f"{source}: expected a mapping of settings, got {type(data).__name__}. "
            "Make the top level a mapping of keys."
        )
    try:
        return cls.model_validate(data)
    except ValidationError as err:
        raise ConfigError(_format_errors(source, err)) from err


def parse_overrides(data: object, source: str) -> SettingsPatch:
    """Validate a mapping (run overrides, a parsed file) as a settings patch."""
    return _validate(SettingsPatch, data, source)


def _read_yaml(path: Path) -> object:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        raise ConfigError(f"{path}: cannot read file ({err}). Check the path exists.") from err
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise ConfigError(f"{path}: invalid YAML ({err}). Fix the syntax and retry.") from err


def load_repo_config(path: Path) -> SettingsPatch:
    """Load a repo's `factory.yaml` as a patch. An empty file is an empty patch."""
    return _validate(SettingsPatch, _read_yaml(path), str(path))


def load_global_settings(path: Path | None, platform: str) -> Settings:
    """Built-in defaults, then the optional global file on top."""
    base = default_settings(platform)
    if path is None:
        return base
    patch = _validate(GlobalPatch, _read_yaml(path), str(path))
    merged = base.model_dump()
    merged["bootstrap"] = base.bootstrap.model_dump(exclude_unset=True)
    merged = _deep_merge(merged, patch.model_dump(exclude_unset=True))
    try:
        return Settings.model_validate(merged)
    except ValidationError as err:
        raise ConfigError(_format_errors(str(path), err)) from err


def dump_config(patch: SettingsPatch) -> str:
    """YAML for the keys that were set, in field order."""
    return yaml.safe_dump(patch.model_dump(exclude_unset=True), sort_keys=False)
