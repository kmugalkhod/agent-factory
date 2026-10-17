"""Progress lines while a command drives a run: one short line per meaningful event.

`start`, `approve` and `resume` run the pipeline in the foreground for minutes. The run's
event log tells a `Progress` about each event once it is written (`EventLog`'s observer), and
it prints a line: the time (HH:MM:SS), the role if there is one, and what happened. Stages,
gates, test runs, state changes, decisions, pauses, needs-you reasons and blocked tool calls
are shown; allowed tool calls, loaded skills, token counts and agent messages only with
`--verbose`, since an agent makes dozens of them.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, tzinfo

from factory_engine import events as ev
from factory_engine.events import Body, Event

MAX_TEXT = 100
"""The most characters of a reason, target or message a line shows."""
MAX_COMMAND = 60
TIME = "%H:%M:%S"
_MORE = "..."


@dataclass(frozen=True)
class Progress:
    """An event observer that writes a progress line for each event worth showing. `tz` is
    the zone times are shown in; None means this computer's."""

    write: Callable[[str], None]
    verbose: bool = False
    tz: tzinfo | None = None

    def __call__(self, event: Event) -> None:
        line = progress_line(event, verbose=self.verbose, tz=self.tz)
        if line is not None:
            self.write(line)


def progress_line(event: Event, *, verbose: bool, tz: tzinfo | None = None) -> str | None:
    """The line for `event`, or None when it isn't shown (at this verbosity)."""
    text = _describe(event.body, verbose=verbose, tz=tz)
    if text is None:
        return None
    who = f" [{event.role}]" if event.role is not None else ""
    return f"{_time(event.at, tz)}{who} {text}"


def _describe(body: Body, *, verbose: bool, tz: tzinfo | None) -> str | None:
    match body:
        case ev.RunCreated(kind=kind, spec=spec):
            return f"run created ({kind}, from {short(spec)})" if spec else f"run created ({kind})"
        case ev.StateChanged(from_state=old, to_state=new):
            opened = " (the pull request is open)" if new == "awaiting_merge" else ""
            return f"state {old} -> {new}{opened}"
        case ev.StageStarted(attempt=attempt):
            return f"started attempt {attempt}"
        case ev.StageFinished(attempt=attempt, outcome=outcome):
            return f"attempt {attempt} {outcome}"
        case ev.GateResult(gate=gate, passed=True):
            return f"gate {gate} passed"
        case ev.GateResult(gate=gate, reason=reason):
            return f"gate {gate} failed: {short(reason)}"
        case ev.TestRun():
            return _tests(body)
        case ev.NeedsYou(reason=reason):
            return f"needs you: {short(reason)}"
        case ev.Paused(reason=reason, detail=detail, resume_at=resume_at):
            until = f"; resumes at {_time(resume_at, tz)}" if resume_at is not None else ""
            return f"paused ({reason}): {short(detail)}{until}"
        case ev.Decision(action=action):
            return f"your decision: {action}"
        case ev.ToolCall(tool=tool, target=target, decision="blocked", reason=reason):
            return f"blocked {_call(tool, target)}: {short(reason or 'no reason given')}"
        case _ if not verbose:
            return None
        case ev.ToolCall(tool=tool, target=target):
            return _call(tool, target)
        case ev.SkillLoaded(skill=skill):
            return f"skill {short(skill)}"
        case ev.Tokens(input=tokens_in, output=tokens_out):
            return f"tokens {tokens_in:,} in, {tokens_out:,} out"
        case ev.Message(sender=sender, text=text):
            return f"{sender}: {short(text)}"
        case _:
            return None


def _tests(run: ev.TestRun) -> str:
    counts = [
        f"{n} {word}"
        for n, word in ((run.passed, "passed"), (run.failed, "failed"))
        if n is not None
    ]
    took = f"{run.seconds:.1f}s"
    result = (
        f"{', '.join(counts)} (exit {run.exit_code}, {took})"
        if counts
        else f"exit {run.exit_code} ({took})"
    )
    return f"tests `{short(run.command, MAX_COMMAND)}`: {result}"


def _call(tool: str, target: str | None) -> str:
    return f"{short(tool)} {short(target)}" if target else short(tool)


def _time(at: datetime, tz: tzinfo | None) -> str:
    return at.astimezone(tz).strftime(TIME)


def short(text: str, limit: int = MAX_TEXT) -> str:
    """`text` on one line: whitespace runs and unprintable characters (newlines, terminal
    escapes) become one space, and it is cut to `limit` characters, ending in `...`."""
    printable = "".join(ch if ch.isprintable() else " " for ch in text)
    flat = " ".join(printable.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - len(_MORE)].rstrip() + _MORE
