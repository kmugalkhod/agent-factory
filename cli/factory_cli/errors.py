"""Errors the `factory` command raises itself. The engine's own errors come from
`factory_engine.errors`; every one of them is a `FactoryError`, which the command prints without
a traceback."""

from factory_engine.errors import FactoryError


class CliError(FactoryError):
    """A command can't do what was asked: no repo in the folder, no such run, a run another
    factory process is driving, or something a later task brings. The message says what to do
    next."""
