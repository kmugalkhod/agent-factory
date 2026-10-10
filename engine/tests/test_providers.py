"""Tests for factory_engine.providers (task 2.6): profiles, the agent environment and preflight."""

from datetime import date, timedelta
from pathlib import Path
from typing import get_args

import pytest

from factory_engine.config import LogicalModel, Settings, default_settings
from factory_engine.errors import ProviderError
from factory_engine.providers import (
    EXPIRY_WARNING_DAYS,
    TOKEN_VAR,
    agent_env,
    get_profile,
    preflight,
    resolve_model,
)

TODAY = date(2026, 10, 10)
TOKEN = "sk-ant-oat01-test-token"
CONFIG_DIR = Path(r"C:\data\agent-factory\config\app-3-builder")

PARENT = {
    # Kept: the system basics an agent's tools need.
    "PATH": r"C:\Windows\system32;C:\Users\me\.local\bin",
    "SystemRoot": r"C:\Windows",
    "TEMP": r"C:\Users\me\AppData\Local\Temp",
    # Removed: anything that could switch the provider or leak credentials.
    "ANTHROPIC_API_KEY": "sk-ant-api-leftover",
    "ANTHROPIC_BASE_URL": "https://proxy.example.com",
    "ANTHROPIC_AUTH_TOKEN": "proxy-token",
    "ANTHROPIC_MODEL": "claude-something-else",
    "HTTPS_PROXY": "http://proxy:8080",
    "http_proxy": "http://proxy:8080",
    "ALL_PROXY": "socks5://proxy:1080",
    "NO_PROXY": "localhost",
    "CLAUDE_CONFIG_DIR": r"C:\Users\me\.claude",
    "CLAUDECODE": "1",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
    "GH_TOKEN": "ghp_secret",
    "GITHUB_TOKEN": "ghp_secret",
    "AWS_ACCESS_KEY_ID": "AKIA...",
    "SOME_TOOL_SETTING": "x",
    TOKEN_VAR: TOKEN,
}


def _settings(**changes: object) -> Settings:
    base = default_settings("win32").model_copy(update={"token_expires": date(2026, 11, 10)})
    return base.model_copy(update=changes)


# ----------------------------------------------------------------- profiles


@pytest.mark.parametrize(
    ("logical", "real"),
    [("opus", "claude-opus-5-5"), ("sonnet", "claude-sonnet-5-5"), ("haiku", "claude-haiku-5-5")],
)
def test_subscription_resolves_logical_models(logical: LogicalModel, real: str) -> None:  # AC2
    assert resolve_model(get_profile("subscription"), logical) == real


def test_every_logical_model_resolves_in_every_profile() -> None:  # AC2
    profile = get_profile("subscription")
    for logical in get_args(LogicalModel):
        assert resolve_model(profile, logical).startswith("claude-")


@pytest.mark.parametrize("name", ["litellm", "bedrock", ""])
def test_litellm_and_unknown_profiles_are_refused(name: str) -> None:
    with pytest.raises(ProviderError) as exc:
        get_profile(name)
    assert "available from milestone 4" in str(exc.value)
    assert repr(name) in str(exc.value)


# -------------------------------------------------------- agent environment


def test_agent_env_drops_anthropic_proxy_and_claude_variables() -> None:  # AC1
    env = agent_env(PARENT, get_profile("subscription"), CONFIG_DIR)
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
        assert name not in env
    assert not [k for k in env if k.upper().startswith("ANTHROPIC_")]
    assert not [k for k in env if "PROXY" in k.upper()]


def test_agent_env_holds_only_the_basics_the_credential_and_the_config_dir() -> None:  # AC1
    env = agent_env(PARENT, get_profile("subscription"), CONFIG_DIR)
    assert env == {
        "PATH": PARENT["PATH"],
        "SYSTEMROOT": PARENT["SystemRoot"],
        "TEMP": PARENT["TEMP"],
        TOKEN_VAR: TOKEN,
        "CLAUDE_CONFIG_DIR": str(CONFIG_DIR),
    }


def test_agent_env_never_passes_the_parents_config_dir() -> None:
    env = agent_env(PARENT, get_profile("subscription"), CONFIG_DIR)
    assert env["CLAUDE_CONFIG_DIR"] == str(CONFIG_DIR)


def test_agent_env_without_the_token_is_refused() -> None:
    parent = {k: v for k, v in PARENT.items() if k != TOKEN_VAR}
    with pytest.raises(ProviderError) as exc:
        agent_env(parent, get_profile("subscription"), CONFIG_DIR)
    assert TOKEN_VAR in str(exc.value)


def test_agent_env_never_contains_the_token_value_in_errors() -> None:
    parent = {**PARENT, TOKEN_VAR: "   "}
    with pytest.raises(ProviderError) as exc:
        agent_env(parent, get_profile("subscription"), CONFIG_DIR)
    assert TOKEN not in str(exc.value)


# ------------------------------------------------------------------ preflight


def test_preflight_passes_and_returns_the_profile() -> None:
    profile = preflight(_settings(), PARENT, today=TODAY)
    assert profile.name == "subscription"


def test_preflight_fails_for_a_missing_token() -> None:  # AC3
    parent = {k: v for k, v in PARENT.items() if k != TOKEN_VAR}
    with pytest.raises(ProviderError) as exc:
        preflight(_settings(), parent, today=TODAY)
    message = str(exc.value)
    assert TOKEN_VAR in message
    assert "claude setup-token" in message


@pytest.mark.parametrize("days_left", [EXPIRY_WARNING_DAYS, 3, 0])
def test_preflight_fails_for_a_near_expiry_token(days_left: int) -> None:  # AC3
    expires = TODAY + timedelta(days=days_left)
    with pytest.raises(ProviderError) as exc:
        preflight(_settings(token_expires=expires), PARENT, today=TODAY)
    message = str(exc.value)
    assert expires.isoformat() in message
    assert "renew" in message.lower()


def test_preflight_fails_for_an_expired_token() -> None:  # AC3
    with pytest.raises(ProviderError) as exc:
        preflight(_settings(token_expires=TODAY - timedelta(days=1)), PARENT, today=TODAY)
    assert "expired" in str(exc.value)


def test_preflight_passes_just_outside_the_warning_window() -> None:
    expires = TODAY + timedelta(days=EXPIRY_WARNING_DAYS + 1)
    assert preflight(_settings(token_expires=expires), PARENT, today=TODAY).name == "subscription"


def test_preflight_fails_when_no_expiry_date_is_recorded() -> None:
    with pytest.raises(ProviderError) as exc:
        preflight(_settings(token_expires=None), PARENT, today=TODAY)
    assert "token_expires" in str(exc.value)


def test_preflight_fails_for_a_disallowed_provider() -> None:  # AC3
    with pytest.raises(ProviderError) as exc:
        preflight(_settings(allowed_providers=["litellm"]), PARENT, today=TODAY)
    message = str(exc.value)
    assert "'subscription'" in message
    assert "litellm" in message
    assert "allowed" in message


def test_preflight_allows_a_listed_provider() -> None:
    settings = _settings(allowed_providers=["litellm", "subscription"])
    assert preflight(settings, PARENT, today=TODAY).name == "subscription"


def test_preflight_refuses_litellm_until_milestone_4() -> None:
    with pytest.raises(ProviderError) as exc:
        preflight(_settings(provider="litellm"), PARENT, today=TODAY)
    assert "available from milestone 4" in str(exc.value)
