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


class AgentError(FactoryError):
    """An agent can't be set up or driven: a missing prompt, no session to resume, or a call
    to an agent that isn't running."""


class SafetyError(FactoryError):
    """A safety check can't be set up, for example a locked file outside the worktree."""


class GitError(FactoryError):
    """A git command failed, timed out or was refused: git missing, a push of a branch that
    isn't the run's own, or a worktree that isn't on its run branch."""


class PushRejectedError(GitError):
    """The safe force-push was refused because the remote run branch changed unexpectedly."""


class GateError(FactoryError):
    """A gate can't take what it needs, for example the post-stage check's baseline."""


class GitHubError(FactoryError):
    """A `gh` command failed, timed out or was refused, for example a merge with no approval."""


class PipelineError(FactoryError):
    """The stage pipeline can't carry a run on, for example a stage that left the run in the
    state it started in."""


class IntakeError(FactoryError):
    """A run can't be started from a request: an empty description, an unreadable spec file or
    a bad name."""


class DecisionError(FactoryError):
    """A decision can't be applied: the run isn't waiting for it, or what you gave is invalid.
    The run is left as it was."""
