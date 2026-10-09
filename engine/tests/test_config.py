"""Tests for factory_engine.config (task 2.1). Each test names the criterion it covers."""

import types
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

import pytest
import yaml
from pydantic import BaseModel

from factory_engine.config import (
    Settings,
    default_settings,
    dump_config,
    load_global_settings,
    load_repo_config,
    merge_settings,
    parse_overrides,
)
from factory_engine.errors import ConfigError, FactoryError

SAMPLE = """\
roles:
  planner:
    model: opus
    effort: high
    write_paths: ["plan.md", "notes.md"]
    deny_paths: ["src/**"]
    shell_read_only: true
    shell_allowlist: []
    skills: ["plan-skill"]
  tester:
    model: haiku
    effort: low
    write_paths: ["spec/**"]
    deny_paths: ["lib/**"]
    shell_read_only: false
    shell_allowlist: ["test"]
    skills: null
  builder:
    model: sonnet
    effort: normal
    write_paths: ["lib/**"]
    deny_paths: ["spec/**", "plan.md"]
    shell_read_only: false
    shell_allowlist: ["test", "build", "lint", "smoke"]
    skills: []
  reviewer:
    model: opus
    effort: high
    write_paths: ["review.md"]
    deny_paths: []
    shell_read_only: true
    shell_allowlist: []
    skills: ["review-skill"]
commands:
  test: "pytest -q"
  build: "make build"
  lint: "ruff check ."
  smoke: null
caps:
  planner_attempts: 2
  tester_attempts: 4
  builder_attempts: 5
  max_turns: 150
  max_minutes: 90
  stall_minutes: 7
provider: bedrock
allowed_providers: ["bedrock", "subscription"]
isolation: sandbox
sandbox_required: true
critical_flows:
  - name: login
    tests: ["tests/test_login.py", "tests/test_session.py"]
  - name: checkout
    tests: []
"""


def _write(tmp_path: Path, text: str, name: str = "factory.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------- AC1, AC2

ROLE_TABLE = [
    ("planner", "opus", "high", ["plan.md"], [], True, [], None),
    ("tester", "sonnet", "normal", ["tests/**"], ["src/**"], False, ["test"], None),
    (
        "builder",
        "sonnet",
        "normal",
        ["src/**"],
        ["tests/**", "plan.md"],
        False,
        ["test", "build", "lint"],
        None,
    ),
    ("reviewer", "opus", "high", ["review.md"], [], False, [], None),
]


@pytest.mark.parametrize("role,model,effort,write,deny,read_only,allowlist,skills", ROLE_TABLE)
def test_default_roles_match_design_table(  # AC1
    role: str,
    model: str,
    effort: str,
    write: list[str],
    deny: list[str],
    read_only: bool,
    allowlist: list[str],
    skills: None,
) -> None:
    r = getattr(default_settings("win32").roles, role)
    assert r.model == model
    assert r.effort == effort
    assert r.write_paths == write
    assert r.deny_paths == deny
    assert r.shell_read_only is read_only
    assert r.shell_allowlist == allowlist
    assert r.skills is skills


def test_default_caps_provider_flags() -> None:  # AC2
    s = default_settings("win32")
    assert s.caps.planner_attempts == 3
    assert s.caps.tester_attempts == 3
    assert s.caps.builder_attempts == 3
    assert s.caps.max_turns == 200
    assert s.caps.max_minutes == 120
    assert s.caps.stall_minutes == 10
    assert s.provider == "subscription"
    assert s.allowed_providers is None
    assert s.sandbox_required is False
    assert s.critical_flows == []
    assert s.commands.test is None
    assert s.commands.build is None
    assert s.commands.lint is None
    assert s.commands.smoke is None


@pytest.mark.parametrize("platform,isolation", [("win32", "worktree"), ("linux", "sandbox")])
def test_default_isolation_by_platform(platform: str, isolation: str) -> None:  # AC2
    assert default_settings(platform).isolation == isolation


# --------------------------------------------------------------------- AC3


def test_bootstrap_override_applies_only_when_bootstrap() -> None:  # AC3
    base = default_settings("win32")
    on = merge_settings(base, bootstrap=True)
    off = merge_settings(base, bootstrap=False)
    assert on.roles.builder.write_paths == ["**"]
    assert on.roles.builder.deny_paths == ["plan.md"]
    assert on.roles.planner == base.roles.planner
    assert on.roles.tester == base.roles.tester
    assert on.roles.reviewer == base.roles.reviewer
    assert off.roles.builder.write_paths == ["src/**"]
    assert off.roles.builder.deny_paths == ["tests/**", "plan.md"]


def test_bootstrap_default_is_data_in_settings() -> None:  # AC3
    patch = default_settings("win32").bootstrap.builder
    assert patch.write_paths == ["**"]
    assert patch.deny_paths == ["plan.md"]


# --------------------------------------------------------------------- AC4


def _leaf_paths(model: type[BaseModel], prefix: tuple[str, ...] = ()) -> list[tuple]:
    leaves: list[tuple[tuple[str, ...], Any]] = []
    for name, field in model.model_fields.items():
        if not prefix and name == "bootstrap":
            continue
        ann = field.annotation
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            leaves.extend(_leaf_paths(ann, (*prefix, name)))
        else:
            leaves.append(((*prefix, name), ann))
    return leaves


def _is_union(origin: Any) -> bool:
    return origin is Union or origin is types.UnionType


def _candidates(ann: Any) -> list[Any]:
    """Three-ish distinct valid values for a leaf of type `ann`."""
    origin = get_origin(ann)
    if _is_union(origin):
        inner = [a for a in get_args(ann) if a is not type(None)]
        return _candidates(inner[0])
    if origin is Literal:
        return list(get_args(ann))
    if ann is bool:
        return [True, False]
    if ann is int:
        return [11, 22, 33]
    if ann is str:
        return ["alpha", "beta", "gamma"]
    if origin is list:
        (item,) = get_args(ann)
        if get_origin(item) is Literal:
            return [[c] for c in get_args(item)]
        if isinstance(item, type) and issubclass(item, BaseModel):
            return [[{"name": f"flow{i}", "tests": [f"t{i}.py"]}] for i in (1, 2, 3)]
        return [["x1"], ["x2"], ["x3"]]
    raise AssertionError(f"no candidate values for {ann!r}")


def _nested(path: tuple[str, ...], value: Any) -> dict[str, Any]:
    out: Any = value
    for key in reversed(path):
        out = {key: out}
    return out


LEAVES = _leaf_paths(Settings)


def _get(settings: Settings, path: tuple[str, ...]) -> Any:
    obj: Any = settings
    for key in path:
        obj = getattr(obj, key)
    if isinstance(obj, list):
        return [o.model_dump() if isinstance(o, BaseModel) else o for o in obj]
    return obj


def test_leaf_enumeration_is_not_trivial() -> None:  # AC4
    paths = {".".join(p) for p, _ in LEAVES}
    assert "roles.builder.model" in paths
    assert "caps.max_turns" in paths
    assert "commands.test" in paths
    assert "critical_flows" in paths
    assert not any(p.startswith("bootstrap") for p in paths)
    assert len(paths) >= 30


@pytest.mark.parametrize("path,ann", LEAVES, ids=[".".join(p) for p, _ in LEAVES])
def test_precedence_every_leaf_field(path: tuple[str, ...], ann: Any) -> None:  # AC4
    vals = _candidates(ann)
    g, r = vals[0], vals[1]
    u = vals[2] if len(vals) > 2 else vals[0]
    assert g != r and u != r
    gp = parse_overrides(_nested(path, g), "global")
    rp = parse_overrides(_nested(path, r), "repo")
    up = parse_overrides(_nested(path, u), "run")
    base = merge_settings(default_settings("win32"), gp)

    assert _get(base, path) == g
    assert _get(merge_settings(base, rp, None), path) == r
    assert _get(merge_settings(base, rp, up), path) == u
    assert _get(merge_settings(base, None, up), path) == u
    assert _get(merge_settings(base, None, None), path) == g


# --------------------------------------------------------------------- AC5


def test_partial_role_patch_keeps_siblings() -> None:  # AC5
    base = default_settings("win32")
    repo = parse_overrides({"roles": {"builder": {"model": "opus"}}}, "repo")
    out = merge_settings(base, repo)
    assert out.roles.builder.model == "opus"
    assert out.roles.builder.model_dump(exclude={"model"}) == base.roles.builder.model_dump(
        exclude={"model"}
    )
    assert out.roles.tester == base.roles.tester
    assert out.caps == base.caps


def test_lists_replace_not_concatenate() -> None:  # AC5
    base = default_settings("win32")
    repo = parse_overrides({"roles": {"builder": {"write_paths": ["lib/**"]}}}, "repo")
    run = parse_overrides({"roles": {"builder": {"deny_paths": ["x"]}}}, "run")
    out = merge_settings(base, repo, run)
    assert out.roles.builder.write_paths == ["lib/**"]
    assert out.roles.builder.deny_paths == ["x"]


def test_explicit_null_overrides() -> None:  # AC5
    base = merge_settings(
        default_settings("win32"),
        parse_overrides(
            {
                "roles": {"builder": {"skills": ["s1"]}},
                "allowed_providers": ["subscription"],
                "commands": {"test": "pytest"},
            },
            "global",
        ),
    )
    assert base.roles.builder.skills == ["s1"]
    repo = parse_overrides(
        {
            "roles": {"builder": {"skills": None}},
            "allowed_providers": None,
            "commands": {"test": None},
        },
        "repo",
    )
    out = merge_settings(base, repo)
    assert out.roles.builder.skills is None
    assert out.allowed_providers is None
    assert out.commands.test is None


# --------------------------------------------------------------------- AC6

BAD_FILES = [
    ("provider: subscription\nbogus: 1\n", "bogus"),
    ("roles:\n  builder:\n    modle: sonnet\n", "roles.builder.modle"),
    ("bootstrap:\n  builder:\n    write_paths: ['**']\n", "bootstrap"),
]


@pytest.mark.parametrize("text,key", BAD_FILES)
def test_unknown_key_reports_file_and_path(  # AC6
    tmp_path: Path, text: str, key: str
) -> None:
    path = _write(tmp_path, text)
    with pytest.raises(ConfigError) as exc:
        load_repo_config(path)
    assert str(path) in str(exc.value)
    assert key in str(exc.value)


WRONG_TYPES = [
    ("caps:\n  builder_attempts: three\n", "caps.builder_attempts"),
    ("critical_flows:\n  - name: a\n    tests: x\n", "critical_flows.0.tests"),
    ("sandbox_required: 'yes'\n", "sandbox_required"),
    ("caps:\n  builder_attempts: '3'\n", "caps.builder_attempts"),
]


@pytest.mark.parametrize("text,key", WRONG_TYPES)
def test_wrong_type_reports_file_and_path(  # AC6
    tmp_path: Path, text: str, key: str
) -> None:
    path = _write(tmp_path, text)
    with pytest.raises(ConfigError) as exc:
        load_repo_config(path)
    assert str(path) in str(exc.value)
    assert key in str(exc.value)


def test_run_overrides_bad_key() -> None:  # AC6
    with pytest.raises(ConfigError) as exc:
        parse_overrides({"caps": {"bogus": 1}}, "run overrides")
    assert "run overrides" in str(exc.value)
    assert "caps.bogus" in str(exc.value)


def test_config_error_is_a_factory_error() -> None:  # AC6
    assert issubclass(ConfigError, FactoryError)
    assert issubclass(FactoryError, Exception)


# --------------------------------------------------------------------- AC7


def test_bad_files_raise_config_error(tmp_path: Path) -> None:  # AC7
    missing = tmp_path / "nope.yaml"
    with pytest.raises(ConfigError) as exc:
        load_repo_config(missing)
    assert str(missing) in str(exc.value)

    bad_yaml = _write(tmp_path, "roles: [unclosed\n  : :\n", "bad.yaml")
    with pytest.raises(ConfigError) as exc:
        load_repo_config(bad_yaml)
    assert str(bad_yaml) in str(exc.value)

    as_list = _write(tmp_path, "- a\n- b\n", "list.yaml")
    with pytest.raises(ConfigError) as exc:
        load_repo_config(as_list)
    assert str(as_list) in str(exc.value)


@pytest.mark.parametrize("text", ["", "   \n", "# only a comment\n"])
def test_empty_file_is_empty_patch(tmp_path: Path, text: str) -> None:  # AC7
    path = _write(tmp_path, text)
    patch = load_repo_config(path)
    base = default_settings("win32")
    assert merge_settings(base, patch) == base
    assert dump_config(patch).strip() in ("", "{}")


# --------------------------------------------------------------------- AC8


def test_logical_model_unresolved(tmp_path: Path) -> None:  # AC8
    path = _write(tmp_path, "roles:\n  builder:\n    model: sonnet\n")
    patch = load_repo_config(path)
    assert patch.roles is not None
    assert patch.roles.builder is not None
    assert patch.roles.builder.model == "sonnet"
    assert merge_settings(default_settings("win32"), patch).roles.builder.model == "sonnet"


def test_real_model_name_rejected(tmp_path: Path) -> None:  # AC8
    path = _write(tmp_path, "roles:\n  tester:\n    model: claude-sonnet-5-5\n")
    with pytest.raises(ConfigError) as exc:
        load_repo_config(path)
    assert "roles.tester.model" in str(exc.value)


# --------------------------------------------------------------------- AC9


def test_full_sample_round_trip(tmp_path: Path) -> None:  # AC9
    path = _write(tmp_path, SAMPLE)
    patch = load_repo_config(path)
    dumped = dump_config(patch)
    assert yaml.safe_load(dumped) == yaml.safe_load(SAMPLE)

    again = _write(tmp_path, dumped, "again.yaml")
    assert load_repo_config(again) == patch


# -------------------------------------------------------------------- AC10


def test_load_global_settings(tmp_path: Path) -> None:  # AC10
    assert load_global_settings(None, "win32") == default_settings("win32")

    good = _write(
        tmp_path,
        "bootstrap:\n  builder:\n    write_paths: ['lib/**']\nprovider: bedrock\n",
        "global.yaml",
    )
    out = load_global_settings(good, "win32")
    assert out.bootstrap.builder.write_paths == ["lib/**"]
    assert out.bootstrap.builder.deny_paths == ["plan.md"]
    assert out.provider == "bedrock"
    assert merge_settings(out, bootstrap=True).roles.builder.write_paths == ["lib/**"]

    bad = _write(tmp_path, "bootstrap:\n  builder:\n    bogus: 1\n", "badglobal.yaml")
    with pytest.raises(ConfigError) as exc:
        load_global_settings(bad, "win32")
    assert str(bad) in str(exc.value)


# ------------------------------------------------- review findings (attempt 1)

NULL_FOR_REQUIRED = [
    ("sandbox_required: null\n", "sandbox_required"),
    ("provider: null\n", "provider"),
    ("roles:\n  builder:\n    model: null\n", "roles.builder.model"),
    ("caps:\n  max_turns: null\n", "caps.max_turns"),
]


@pytest.mark.parametrize("text,key", NULL_FOR_REQUIRED)
def test_null_for_required_field_names_file(  # Minor 1
    tmp_path: Path, text: str, key: str
) -> None:
    path = _write(tmp_path, text)
    with pytest.raises(ConfigError) as exc:
        load_repo_config(path)
    assert str(path) in str(exc.value)
    assert key in str(exc.value)


@pytest.mark.parametrize(
    "data,key",
    [
        ({"sandbox_required": None}, "sandbox_required"),
        ({"roles": {"builder": {"model": None}}}, "roles.builder.model"),
    ],
)
def test_null_for_required_field_names_run_source(  # Minor 1
    data: dict[str, Any], key: str
) -> None:
    with pytest.raises(ConfigError) as exc:
        parse_overrides(data, "run overrides")
    assert "run overrides" in str(exc.value)
    assert key in str(exc.value)


def test_non_utf8_file_names_file(tmp_path: Path) -> None:  # Minor 2
    path = tmp_path / "factory.yaml"
    path.write_bytes(b"provider: \xff\xfe\x80\n")
    with pytest.raises(ConfigError) as exc:
        load_repo_config(path)
    assert str(path) in str(exc.value)


def test_non_utf8_global_file_names_file(tmp_path: Path) -> None:  # Minor 2
    path = tmp_path / "global.yaml"
    path.write_bytes(b"provider: \xff\xfe\x80\n")
    with pytest.raises(ConfigError) as exc:
        load_global_settings(path, "win32")
    assert str(path) in str(exc.value)


CAP_FIELDS = [
    "planner_attempts",
    "tester_attempts",
    "builder_attempts",
    "max_turns",
    "max_minutes",
    "stall_minutes",
]


@pytest.mark.parametrize("value", [0, -1])
@pytest.mark.parametrize("field", CAP_FIELDS)
def test_caps_reject_non_positive_in_file(  # Minor 3
    tmp_path: Path, field: str, value: int
) -> None:
    path = _write(tmp_path, f"caps:\n  {field}: {value}\n")
    with pytest.raises(ConfigError) as exc:
        load_repo_config(path)
    assert str(path) in str(exc.value)
    assert f"caps.{field}" in str(exc.value)


@pytest.mark.parametrize("value", [0, -5])
def test_caps_reject_non_positive_in_run_overrides(value: int) -> None:  # Minor 3
    with pytest.raises(ConfigError) as exc:
        parse_overrides({"caps": {"builder_attempts": value}}, "run overrides")
    assert "run overrides" in str(exc.value)
    assert "caps.builder_attempts" in str(exc.value)


def test_caps_accept_one() -> None:  # Minor 3
    patch = parse_overrides({"caps": {"builder_attempts": 1}}, "run overrides")
    out = merge_settings(default_settings("win32"), patch)
    assert out.caps.builder_attempts == 1
