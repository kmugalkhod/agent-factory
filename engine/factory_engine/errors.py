"""Exception classes for the engine. Messages say what failed and what to do next."""


class FactoryError(Exception):
    """Base class for every error the engine raises on purpose."""


class ConfigError(FactoryError):
    """A config file or override is missing, unreadable or invalid."""


class DataFolderError(FactoryError):
    """The factory data folder can't be located, or a repo name or slug is unsafe as a path."""


class RunFileError(FactoryError):
    """A run's `run.json` is missing, unreadable, invalid or couldn't be written."""


class RegistryError(FactoryError):
    """The SQLite registry can't be opened, migrated, read or written."""


class EventLogError(FactoryError):
    """A run's `events.jsonl` can't be read or written, or holds an invalid event."""


class IllegalTransitionError(FactoryError):
    """A run was asked to move between two states the state machine doesn't connect."""


class ProviderError(FactoryError):
    """A provider profile is unknown, not allowed, or its credentials aren't usable."""


class AgentConfigError(FactoryError):
    """An agent's config folder can't be built: an unknown skill, a path no rule can express,
    or a file that can't be written."""


class SafetyError(FactoryError):
    """A safety check can't be set up, for example a locked file outside the worktree."""
