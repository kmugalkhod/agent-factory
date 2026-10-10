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
