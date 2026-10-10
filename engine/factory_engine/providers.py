"""Provider profiles, each agent's environment, and the provider preflight before a run.

A profile maps the logical models roles ask for (`opus`, `sonnet`, `haiku`) to real model
names, and names the credential variables it needs. Only `subscription` exists until
milestone 4.

An agent's environment is built from a clean base, never from the parent's: a short list of
system variables an agent's tools need, then the profile's own credentials, then the agent's
`CLAUDE_CONFIG_DIR`. Anything else in the parent, such as a leftover `ANTHROPIC_*` or proxy
variable, would silently switch the provider, so it never reaches the agent.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from types import MappingProxyType

from factory_engine.config import LogicalModel, Settings
from factory_engine.errors import ProviderError

TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"
CONFIG_DIR_VAR = "CLAUDE_CONFIG_DIR"
EXPIRY_WARNING_DAYS = 14

# System variables an agent's tools (shell, uv, git) need. Matched without regard to case.
BASE_VARS = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "HOME",
    "LOCALAPPDATA",
    "APPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "COMMONPROGRAMFILES",
    "USERNAME",
    "COMPUTERNAME",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "OS",
    "LANG",
    "LC_ALL",
    "TERM",
)


@dataclass(frozen=True)
class Profile:
    name: str
    models: Mapping[LogicalModel, str]
    credentials: tuple[str, ...]


PROFILES: Mapping[str, Profile] = MappingProxyType(
    {
        "subscription": Profile(
            name="subscription",
            models=MappingProxyType(
                {
                    "opus": "claude-opus-5-5",
                    "sonnet": "claude-sonnet-5-5",
                    "haiku": "claude-haiku-5-5",
                }
            ),
            credentials=(TOKEN_VAR,),
        ),
    }
)


def get_profile(name: str) -> Profile:
    profile = PROFILES.get(name)
    if profile is None:
        kind = "the litellm profile" if name == "litellm" else "an unknown provider profile"
        raise ProviderError(
            f"provider {name!r} is {kind}; it is available from milestone 4. "
            f"Use one of: {', '.join(sorted(PROFILES))}."
        )
    return profile


def resolve_model(profile: Profile, logical: LogicalModel) -> str:
    return profile.models[logical]


def agent_env(parent: Mapping[str, str], profile: Profile, config_dir: Path) -> dict[str, str]:
    """The environment for one agent: system basics, the profile's credentials, its config dir.

    `parent` is the engine's environment (pass `os.environ`); nothing else is copied from it.
    """
    by_upper = {key.upper(): value for key, value in parent.items()}
    env = {name: by_upper[name] for name in BASE_VARS if name in by_upper}
    for name in profile.credentials:
        value = by_upper.get(name, "").strip()
        if not value:
            raise ProviderError(_missing_credential(profile, name))
        env[name] = value
    env[CONFIG_DIR_VAR] = str(config_dir)
    return env


def preflight(settings: Settings, parent: Mapping[str, str], *, today: date) -> Profile:
    """Check the run's provider before it starts. Returns its profile, or raises ProviderError.

    The provider must be allowed for the repo and known, its credentials set, and the
    subscription token's recorded expiry date more than 14 days away.
    """
    name = settings.provider
    allowed = settings.allowed_providers
    if allowed is not None and name not in allowed:
        raise ProviderError(
            f"provider {name!r} is not allowed for this repo; its allowed providers are: "
            f"{', '.join(allowed) or 'none'}. Pick an allowed provider for the run."
        )
    profile = get_profile(name)
    by_upper = {key.upper(): value for key, value in parent.items()}
    for credential in profile.credentials:
        if not by_upper.get(credential, "").strip():
            raise ProviderError(_missing_credential(profile, credential))
    if TOKEN_VAR in profile.credentials:
        _check_expiry(settings.token_expires, today)
    return profile


def _check_expiry(expires: date | None, today: date) -> None:
    if expires is None:
        raise ProviderError(
            "the subscription token's expiry date isn't recorded. Add `token_expires: "
            "YYYY-MM-DD` to the global settings file (the date `claude setup-token` gave)."
        )
    if expires < today:
        raise ProviderError(
            f"the subscription token expired on {expires.isoformat()}. Run "
            "`claude setup-token`, store the new token, and record its expiry date."
        )
    if expires - today <= timedelta(days=EXPIRY_WARNING_DAYS):
        raise ProviderError(
            f"the subscription token expires on {expires.isoformat()}, within "
            f"{EXPIRY_WARNING_DAYS} days. Renew it with `claude setup-token` and record the "
            "new expiry date before starting a run."
        )


def _missing_credential(profile: Profile, name: str) -> str:
    hint = " Run `claude setup-token` and store the token in it." if name == TOKEN_VAR else ""
    return f"provider {profile.name!r} needs {name}, which isn't set.{hint}"
