"""Exception classes for the engine. Messages say what failed and what to do next."""


class FactoryError(Exception):
    """Base class for every error the engine raises on purpose."""


class ConfigError(FactoryError):
    """A config file or override is missing, unreadable or invalid."""
