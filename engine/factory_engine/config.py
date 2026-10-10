"""Settings models, defaults, merge (run > repo > global) and load/dump of config files."""

from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from factory_engine.errors import ConfigError

LogicalModel = Literal["opus", "sonnet", "haiku"]
Effort = Literal["low", "normal", "high"]
CommandName = Literal["test", "build", "lint", "smoke"]
PositiveCap = Annotated[int, Field(gt=0)]


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
    planner_attempts: PositiveCap
    tester_attempts: PositiveCap
    builder_attempts: PositiveCap
    max_turns: PositiveCap
    max_minutes: PositiveCap
    stall_minutes: PositiveCap


class CriticalFlow(_Model):
    name: str
    tests: list[str]


# Patch fields that `Settings` doesn't allow to be null are typed without `None` and default
# to `Field(default=None)`. Pydantic doesn't validate defaults, so an unset field passes,
# while an explicit null fails at load time with the file and key path. Only `exclude_unset`
# dumps are used for patches, so the placeholder never reaches a merge.
def _unset() -> Any:
    return Field(default=None)


class RolePatch(_Model):
    model: LogicalModel = _unset()
    effort: Effort = _unset()
    write_paths: list[str] = _unset()
    deny_paths: list[str] = _unset()
    shell_read_only: bool = _unset()
    shell_allowlist: list[CommandName] = _unset()
    skills: list[str] | None = None


class RolesPatch(_Model):
    planner: RolePatch = _unset()
    tester: RolePatch = _unset()
    builder: RolePatch = _unset()
    reviewer: RolePatch = _unset()


class CommandsPatch(_Model):
    test: str | None = None
    build: str | None = None
    lint: str | None = None
    smoke: str | None = None


class CapsPatch(_Model):
    planner_attempts: PositiveCap = _unset()
    tester_attempts: PositiveCap = _unset()
    builder_attempts: PositiveCap = _unset()
    max_turns: PositiveCap = _unset()
    max_minutes: PositiveCap = _unset()
    stall_minutes: PositiveCap = _unset()


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
    roles: RolesPatch = _unset()
    commands: CommandsPatch = _unset()
    caps: CapsPatch = _unset()
    provider: str = _unset()
    allowed_providers: list[str] | None = None
    isolation: Literal["worktree", "sandbox"] = _unset()
    sandbox_required: bool = _unset()
    critical_flows: list[CriticalFlow] = _unset()


class GlobalPatch(SettingsPatch):
    bootstrap: Bootstrap = _unset()


def default_settings(platform: str) -> Settings:
    """Built-in defaults. `platform` is `sys.platform`; passed in so tests can vary it."""
    return Settings.model_validate(
        {
            "roles": {
                "planner": _role("opus", "high", ["plan.md"], [], True, []),
                "tester": _role(
                    "sonnet", "normal", ["tests/**"], ["src/**"], False, ["test", "lint"]
                ),
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


def _settings_dict(settings: Settings) -> dict[str, Any]:
    """Full dump, except `bootstrap` keeps only the keys that were set (it is a patch)."""
    out = settings.model_dump()
    out["bootstrap"] = settings.bootstrap.model_dump(exclude_unset=True)
    return out


def merge_settings(
    global_settings: Settings,
    repo: SettingsPatch | None = None,
    run: SettingsPatch | None = None,
    *,
    bootstrap: bool = False,
) -> Settings:
    """Merge run > repo > global. With `bootstrap`, the builder patch sits above global."""
    merged = _settings_dict(global_settings)
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
    except UnicodeDecodeError as err:
        raise ConfigError(f"{path}: not valid UTF-8 ({err}). Save the file as UTF-8.") from err
    try:
        _check_nodes(yaml.compose(text, Loader=yaml.SafeLoader), path, [], set(), set())
        return yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise ConfigError(f"{path}: invalid YAML ({err}). Fix the syntax and retry.") from err


def _check_nodes(
    node: yaml.Node | None, path: Path, keys: list[str], open_ids: set[int], done_ids: set[int]
) -> None:
    """Fail on a repeated mapping key (loaders keep the last one silently) or an alias cycle.

    Aliases make the node tree a graph: `open_ids` holds the nodes on the current path, so
    meeting one again is a cycle; `done_ids` skips shared nodes already checked.
    """
    if not isinstance(node, yaml.CollectionNode) or id(node) in done_ids:
        return
    if id(node) in open_ids:
        line = node.start_mark.line + 1
        raise ConfigError(
            f"{path}: {'.'.join(keys)}: alias refers to itself (line {line}). "
            "Write the values out instead of the alias."
        )
    open_ids.add(id(node))
    if isinstance(node, yaml.MappingNode):
        seen: set[str] = set()
        for key_node, value_node in node.value:
            key = str(key_node.value)
            if key in seen:
                line = key_node.start_mark.line + 1
                raise ConfigError(
                    f"{path}: {'.'.join([*keys, key])}: duplicate key (line {line}). "
                    "Keep one entry and merge them."
                )
            seen.add(key)
            _check_nodes(value_node, path, [*keys, key], open_ids, done_ids)
    else:
        for index, item in enumerate(node.value):
            _check_nodes(item, path, [*keys, str(index)], open_ids, done_ids)
    open_ids.discard(id(node))
    done_ids.add(id(node))


def load_repo_config(path: Path) -> SettingsPatch:
    """Load a repo's `factory.yaml` as a patch. An empty file is an empty patch."""
    return _validate(SettingsPatch, _read_yaml(path), str(path))


def load_global_settings(path: Path | None, platform: str) -> Settings:
    """Built-in defaults, then the optional global file on top."""
    base = default_settings(platform)
    if path is None:
        return base
    patch = _validate(GlobalPatch, _read_yaml(path), str(path))
    merged = _deep_merge(_settings_dict(base), patch.model_dump(exclude_unset=True))
    try:
        return Settings.model_validate(merged)
    except ValidationError as err:
        raise ConfigError(_format_errors(str(path), err)) from err


def dump_config(patch: SettingsPatch) -> str:
    """YAML for the keys that were set, in field order."""
    return yaml.safe_dump(patch.model_dump(exclude_unset=True), sort_keys=False)
