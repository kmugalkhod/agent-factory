"""The agent runner: build one stage agent's options, run it, and record what it does.

- `build_spec` gathers everything one agent needs: its worktree, system prompt (the factory's
  `prompts/<role>.md`, the bootstrap addendum on run 0, then the repo's
  `.claude/agents/<role>.md`), model and thinking effort, a clean environment, the safety hook,
  its tools and the session to resume.
- `AgentRunner` runs a spec. `ClaudeAgentRunner` does it through the Claude Agent SDK;
  `FakeAgentRunner` (in `fake.py`) replays scripted messages for tests.
- `SessionRecorder` turns the agent's messages into the run's record, the same way for both:
  its text becomes `message` events, its session ID is saved to `run.json` and the registry as
  soon as it is known, and each turn's usage becomes a `tokens` event, a dated `token_use`
  record of its attempt and part of the role's total. Listeners (the watchdog, `watchdog.py`)
  see every message after it is recorded.

A runner drives one agent at a time. While it runs, `interrupt` stops the current turn and
`send` gives the agent a note: a turn in progress is interrupted first, then the agent carries
on in the same session with the note.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol, cast

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    TextBlock,
)
from claude_agent_sdk.types import EffortLevel, SettingSource
from claude_agent_sdk.types import HookCallback as SdkHookCallback

from factory_engine.config import Effort, Settings
from factory_engine.errors import AgentError
from factory_engine.events import Message, Tokens
from factory_engine.metrics import token_use
from factory_engine.providers import Profile, agent_env, resolve_model
from factory_engine.run import Role, Run, save_run
from factory_engine.safety.hook import EventSink, HookCallback
from factory_engine.safety.secrets import redact

PROMPT_SUFFIX = ".md"
BOOTSTRAP_PROMPT = "bootstrap"
BOOTSTRAP_ROLES: frozenset[Role] = frozenset({"planner", "builder"})
REPO_AGENTS = Path(".claude") / "agents"

# Built-in tools an agent is offered. Everything else (web, subagents, MCP) isn't loaded at all;
# the safety hook still checks each call to these.
BASE_TOOLS = ("Read", "Glob", "Grep", "Write", "Edit", "NotebookEdit", "Skill", "TodoWrite")
SHELL_TOOLS = ("Bash", "PowerShell")
EFFORT: Mapping[Effort, EffortLevel] = {"low": "low", "normal": "medium", "high": "high"}
# Variables the SDK sets for the CLI itself; blanking an inherited copy would undo them.
SDK_VARS = frozenset(
    {"CLAUDE_CODE_ENTRYPOINT", "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_CODE_SDK_READS_SESSION_STATE"}
)
# The agent's config folder (`CLAUDE_CONFIG_DIR`), then the repo's settings and `CLAUDE.md`.
SETTING_SOURCES: tuple[SettingSource, ...] = ("user", "project")

FRONTMATTER = re.compile(r"\A﻿?---[ \t]*\r?\n.*?^---[ \t]*(\r?\n|\Z)", re.DOTALL | re.MULTILINE)

Outcome = Literal["completed", "failed", "interrupted"]


@dataclass(frozen=True)
class AgentSpec:
    """Everything one agent run needs. Build it with `build_spec`."""

    role: Role
    attempt: int
    prompt: str
    worktree: Path
    run_folder: Path
    system_prompt: str
    model: str
    effort: Effort
    env: Mapping[str, str]
    tools: tuple[str, ...]
    skills: tuple[str, ...] | None  # None: every skill in the config folder and the repo
    resume: str | None
    max_turns: int
    hook: HookCallback


def build_spec(
    *,
    role: Role,
    attempt: int,
    prompt: str,
    run: Run,
    settings: Settings,
    profile: Profile,
    parent_env: Mapping[str, str],
    worktree: Path,
    run_folder: Path,
    config_dir: Path,
    prompts_dir: Path,
    hook: HookCallback,
    resume: bool,
) -> AgentSpec:
    """The spec for `role`'s agent on `run`.

    `settings` are the run's effective settings (bootstrap applied for run 0), `parent_env` the
    engine's environment, `config_dir` the agent's folder from `write_agent_config`, and `hook`
    its safety hook from `make_safety_hook`. With `resume`, the agent continues the role's
    saved session.
    """
    if attempt < 1:
        raise AgentError(f"{role}: attempt {attempt} isn't valid; attempts count from 1.")
    session = run.session_ids.get(role)
    if resume and session is None:
        raise AgentError(
            f"run {run.repo}/{run.id}: the {role} has no saved session to resume. "
            "Start a fresh session instead."
        )
    role_settings = getattr(settings.roles, role)
    has_shell = role_settings.shell_read_only or bool(role_settings.shell_allowlist)
    return AgentSpec(
        role=role,
        attempt=attempt,
        prompt=prompt,
        worktree=worktree,
        run_folder=run_folder,
        system_prompt=system_prompt(role, prompts_dir, worktree, bootstrap=run.kind == "bootstrap"),
        model=resolve_model(profile, role_settings.model),
        effort=role_settings.effort,
        env=agent_env(parent_env, profile, config_dir),
        tools=BASE_TOOLS + (SHELL_TOOLS if has_shell else ()),
        skills=None if role_settings.skills is None else tuple(role_settings.skills),
        resume=session if resume else None,
        max_turns=settings.caps.max_turns,
        hook=hook,
    )


def system_prompt(role: Role, prompts_dir: Path, worktree: Path, *, bootstrap: bool) -> str:
    """The factory's prompt for `role`, the bootstrap addendum on a bootstrap run (planner and
    builder), then the repo's own prompt for the role without its frontmatter: the model always
    comes from `factory.yaml`."""
    parts = [_read_prompt(prompts_dir / f"{role}{PROMPT_SUFFIX}")]
    if bootstrap and role in BOOTSTRAP_ROLES:
        parts.append(_read_prompt(prompts_dir / f"{BOOTSTRAP_PROMPT}{PROMPT_SUFFIX}"))
    repo_prompt = _repo_prompt(worktree, role)
    if repo_prompt:
        parts.append(repo_prompt)
    return "\n\n".join(part.strip() for part in parts) + "\n"


def _read_prompt(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise AgentError(
            f"{path}: can't read the prompt ({err}). Check the factory's prompts folder."
        ) from err


def _repo_prompt(worktree: Path, role: Role) -> str | None:
    """The body of the repo's `.claude/agents/<role>.md`, or None when there isn't one. A file
    that leads outside the worktree (a link) is refused: it would put that file in the prompt."""
    path = worktree / REPO_AGENTS / f"{role}{PROMPT_SUFFIX}"
    if not path.exists():
        return None
    try:
        resolved = path.resolve(strict=True)
        root = worktree.resolve(strict=True)
    except OSError as err:
        raise AgentError(f"{path}: can't read the repo's {role} prompt ({err}).") from err
    if not resolved.is_relative_to(root):
        raise AgentError(
            f"{path}: the repo's {role} prompt leads outside the worktree, to {resolved}. "
            "Make it a plain file in the repo."
        )
    return FRONTMATTER.sub("", _read_prompt(path), count=1).strip() or None


def sdk_options(spec: AgentSpec, parent_env: Mapping[str, str]) -> ClaudeAgentOptions:
    """The SDK options for `spec`.

    The SDK starts the CLI with the engine's own environment and lays `env` over it, so every
    variable in `parent_env` that the agent's clean environment leaves out is set to an empty
    value here: a leftover `ANTHROPIC_*` or proxy setting never reaches the agent.
    """
    env = dict(spec.env)
    kept = {name.upper() for name in env} | SDK_VARS
    blanked = {name: "" for name in parent_env if name.upper() not in kept}
    return ClaudeAgentOptions(
        cwd=spec.worktree,
        system_prompt={"type": "preset", "preset": "claude_code", "append": spec.system_prompt},
        model=spec.model,
        effort=EFFORT[spec.effort],
        thinking={"type": "adaptive"},
        env={**blanked, **env},
        # The hook is typed loosely so `safety` needn't import the SDK; it takes and returns
        # the same dicts.
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[cast(SdkHookCallback, spec.hook)])]},
        tools=list(spec.tools),
        skills="all" if spec.skills is None else list(spec.skills),
        setting_sources=list(SETTING_SOURCES),
        permission_mode="dontAsk",
        add_dirs=[spec.run_folder],
        resume=spec.resume,
        max_turns=spec.max_turns,
    )


# ------------------------------------------------------------------ recording


class RunEvents(EventSink, Protocol):
    """The run's event log (an `EventLog`)."""

    @property
    def repo(self) -> str: ...

    @property
    def run_id(self) -> int: ...


class RunIndex(Protocol):
    """Where the run is indexed (the `Registry`)."""

    def upsert_run(self, run: Run) -> None: ...


@dataclass(frozen=True)
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_creation: int = 0

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_creation

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input + other.input,
            self.output + other.output,
            self.cache_read + other.cache_read,
            self.cache_creation + other.cache_creation,
        )


@dataclass(frozen=True)
class AgentResult:
    run: Run  # the run as last saved
    session_id: str | None
    usage: Usage  # this attempt's tokens
    outcome: Outcome
    result: str | None  # the agent's final answer, if it finished


class SessionRecorder:
    """Records one agent attempt into its run: events, session ID and tokens."""

    def __init__(
        self,
        run: Run,
        *,
        run_folder: Path,
        events: RunEvents,
        registry: RunIndex,
        role: Role,
        attempt: int,
        clock: Callable[[], datetime],
    ) -> None:
        if attempt < 1:
            raise AgentError(f"{role}: attempt {attempt} isn't valid; attempts count from 1.")
        if (events.repo, events.run_id) != (run.repo, run.id):
            raise AgentError(
                f"event log for run {events.repo}/{events.run_id} can't record run "
                f"{run.repo}/{run.id}. Pass the run's own event log."
            )
        self.role: Role = role
        self.attempt = attempt
        self._run = run
        self._folder = run_folder
        self._events = events
        self._registry = registry
        self._clock = clock
        self._usage = Usage()
        self._last: ResultMessage | None = None
        self._listeners: list[Callable[[object], None]] = []

    @property
    def run(self) -> Run:
        return self._run

    @property
    def session_id(self) -> str | None:
        return self._run.session_ids.get(self.role)

    def listen(self, listener: Callable[[object], None]) -> None:
        """Call `listener` with every message from the agent, after it is recorded."""
        self._listeners.append(listener)

    def save(self, run: Run) -> None:
        """Make `run` (this run, changed by the engine) the current one: save it to `run.json`
        and the registry."""
        if (run.repo, run.id) != (self._run.repo, self._run.id):
            raise AgentError(
                f"the recorder for run {self._run.repo}/{self._run.id} can't save run "
                f"{run.repo}/{run.id}."
            )
        save_run(run, self._folder)
        self._run = run  # run.json is the truth, even if indexing it fails
        self._registry.upsert_run(run)

    def record(self, message: object) -> None:
        """Take in one message from the agent."""
        self._record(message)
        for listener in self._listeners:
            listener(message)

    def _record(self, message: object) -> None:
        if isinstance(message, SystemMessage):
            if message.subtype == "init":
                session = message.data.get("session_id")
                if self._is_new_session(session):
                    self._save(session=session)
        elif isinstance(message, AssistantMessage):
            if message.parent_tool_use_id is None:  # not a subagent's
                text = "\n\n".join(b.text for b in message.content if isinstance(b, TextBlock))
                if text.strip():
                    self._events.append(Message(sender="agent", text=redact(text)), role=self.role)
        elif isinstance(message, ResultMessage):
            self._last = message
            usage = _usage(message.usage)
            self._usage += usage
            self._events.append(
                Tokens(
                    attempt=self.attempt,
                    input=usage.input,
                    output=usage.output,
                    cache_read=usage.cache_read,
                    cache_creation=usage.cache_creation,
                ),
                role=self.role,
            )
            session = message.session_id
            self._save(session=session if self._is_new_session(session) else None, usage=usage)

    def sent(self, text: str) -> None:
        """Log a note you sent the agent, with any secret in it masked."""
        self._events.append(Message(sender="you", text=redact(text)), role=self.role)

    def finish(self, *, interrupted: bool) -> AgentResult:
        if interrupted:
            outcome: Outcome = "interrupted"
        elif self._last is None or self._last.is_error:
            outcome = "failed"
        else:
            outcome = "completed"
        return AgentResult(
            run=self._run,
            session_id=self.session_id,
            usage=self._usage,
            outcome=outcome,
            result=self._last.result if self._last is not None else None,
        )

    def _is_new_session(self, session: object) -> bool:
        return isinstance(session, str) and bool(session) and session != self.session_id

    def _save(self, *, session: object = None, usage: Usage | None = None) -> None:
        now = self._clock()
        update: dict[str, object] = {"updated_at": now}
        if isinstance(session, str):
            update["session_ids"] = {**self._run.session_ids, self.role: session}
        if usage is not None:
            update["metrics"] = token_use(
                self._run.metrics,
                role=self.role,
                attempt=self.attempt,
                at=now,
                input=usage.input,
                output=usage.output,
                cache_read=usage.cache_read,
                cache_creation=usage.cache_creation,
            )
        self.save(self._run.model_copy(update=update))


def persist(run: Run, run_folder: Path, registry: RunIndex) -> None:
    """Save `run` to its `run.json`, then index it."""
    save_run(run, run_folder)
    registry.upsert_run(run)


def _usage(raw: Mapping[str, object] | None) -> Usage:
    def count(key: str) -> int:
        value = raw.get(key) if raw else None
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0

    return Usage(
        input=count("input_tokens"),
        output=count("output_tokens"),
        cache_read=count("cache_read_input_tokens"),
        cache_creation=count("cache_creation_input_tokens"),
    )


# -------------------------------------------------------------------- runners


class AgentRunner(Protocol):
    """Runs one agent at a time and lets the engine talk to it while it runs."""

    async def run(self, spec: AgentSpec, recorder: SessionRecorder) -> AgentResult:
        """Run the agent until it finishes its turns; record everything through `recorder`."""
        ...

    async def interrupt(self) -> None:
        """Stop the running agent's current turn."""
        ...

    async def send(self, text: str) -> None:
        """Give the running agent a note; a turn in progress is interrupted first."""
        ...


def check_pairing(spec: AgentSpec, recorder: SessionRecorder) -> None:
    if (spec.role, spec.attempt) != (recorder.role, recorder.attempt):
        raise AgentError(
            f"the spec is for the {spec.role} attempt {spec.attempt} but the recorder for the "
            f"{recorder.role} attempt {recorder.attempt}. Pass a recorder for the same attempt."
        )


def not_running() -> AgentError:
    return AgentError("no agent is running on this runner; resume its session instead.")


@dataclass
class _Active:
    client: ClaudeSDKClient
    recorder: SessionRecorder
    turns: int = 1  # user messages sent; each ends with one result
    results: int = 0
    interrupted: bool = False

    @property
    def busy(self) -> bool:
        return self.results < self.turns


class ClaudeAgentRunner:
    """Runs agents through the Claude Agent SDK.

    `parent_env` is the engine's environment (pass `os.environ`); it is only used to blank what
    the agent mustn't inherit. `client_factory` makes the SDK client; tests pass one with a fake
    transport.
    """

    def __init__(
        self,
        parent_env: Mapping[str, str],
        *,
        client_factory: Callable[[ClaudeAgentOptions], ClaudeSDKClient] = ClaudeSDKClient,
    ) -> None:
        self._parent_env = parent_env
        self._client_factory = client_factory
        self._active: _Active | None = None

    async def run(self, spec: AgentSpec, recorder: SessionRecorder) -> AgentResult:
        if self._active is not None:
            raise AgentError(
                "an agent is already running on this runner; use one runner per agent."
            )
        check_pairing(spec, recorder)
        client = self._client_factory(sdk_options(spec, self._parent_env))
        active = self._active = _Active(client, recorder)
        try:
            await client.connect()
            await client.query(spec.prompt)
            async for message in client.receive_messages():
                recorder.record(message)
                if isinstance(message, ResultMessage):
                    active.results += 1
                    if not active.busy:
                        break
        finally:
            self._active = None  # before any await, so a late `send` can't reach a closed client
            await client.disconnect()
        return recorder.finish(interrupted=active.interrupted)

    async def interrupt(self) -> None:
        active = self._active
        if active is None:
            raise not_running()
        active.interrupted = True
        await active.client.interrupt()

    async def send(self, text: str) -> None:
        active = self._active
        if active is None:
            raise not_running()
        active.recorder.sent(text)  # a note that can't be logged isn't sent
        # Count the note's turn before any await, so the run waits for its result too.
        active.turns += 1
        active.interrupted = False
        try:
            # A note mid-turn would be folded into that turn; stop the turn so the note starts
            # one. The runner is only active while a turn is in progress.
            await active.client.interrupt()
            await active.client.query(text)
        except BaseException:
            # The note's turn never started, so don't wait for its result. The current turn
            # may have been stopped already; the attempt can't count as completed.
            active.turns -= 1
            active.interrupted = True
            raise
