"""The run's `report.md` (task 2.27; `templates/report.md`, its headings a contract).

The engine writes it into the run's folder whenever the run ends or waits for you with its
work done: awaiting merge, merged, needs you, failed or stopped (the template's statuses); not
while it waits for plan approval or is paused. It is rewritten each time, so it always shows
the run as last saved.

Everything in it comes from the run folder and `run.json`, with no model involved:

- Summary: the request's first line (`intent.md`), how many files the builder touched
  (`handoff-builder.md`), and what to look at: why the run needs you, or the open findings.
- Acceptance criteria: each criterion of `plan.md`, its state in the last `review.md` and the
  tests the plan's Test cases name for it.
- Tests: the last test command the engine ran (the `test_run` events), its result, and how many
  tests the tester added (`tester-cases.json`).
- Review: the last verdict and every finding in it, which the run leaves open.
- Run: provider and models, isolation, builder attempts, tokens per role and per attempt,
  first pass, retries, Important findings, usage-limit pauses, the lines you changed on the
  pull request, and the duration.
- Follow-ups: the builder's Gaps.

It fits on one page (`MAX_LINES`): long lists are cut with a pointer to the file that holds the
rest, and long lines are shortened. Text taken from the run's files is made one line, so it
can't add a heading or a code fence.
"""

import re
from collections.abc import Sequence
from pathlib import Path

from factory_engine.config import Settings
from factory_engine.errors import FactoryError, ReportError
from factory_engine.events import Event, NeedsYou, TestRun, read_events
from factory_engine.gates.plan import PLAN_FILE, check_plan
from factory_engine.gates.review import Finding, check_review_file
from factory_engine.gates.tests_gate import load_tester_cases
from factory_engine.metrics import describe_tokens, tokens_per_attempt, usage_limit_hits
from factory_engine.providers import Profile, resolve_model
from factory_engine.run import Role, Run, RunState
from factory_engine.testrun import write_atomic

REPORT_FILE = "report.md"
INTENT_FILE = "intent.md"
REQUEST_HEADING = "## Request"
REVIEW_FILE = "review.md"
HANDOFF_BUILDER = "handoff-builder.md"
HEADINGS = ("Summary", "Acceptance criteria", "Tests", "Review", "Run", "Follow-ups")
MAX_LINES = 60  # one page (templates/README.md)
MAX_CRITERIA = 12
MAX_FINDINGS = 8
MAX_FOLLOW_UPS = 5
MAX_ATTEMPTS_SHOWN = 10
MAX_LINE = 300  # characters of one line
ROLES: tuple[Role, ...] = ("planner", "tester", "builder", "reviewer")
STATUS: dict[RunState, str] = {
    "awaiting_merge": "awaiting merge",
    "merged": "merged",
    "needs_you": "needs you",
    "failed": "failed",
    "stopped": "stopped",
}

_CASE = re.compile(r"^\s*[-*+]\s+AC(\d+):\s+(.*)$")
_NAME = re.compile(r"`([^`]+)`")
_JUDGED = re.compile(r"^- AC(\d+): (met|not met)\.")
_BULLET = re.compile(r"^\s*[-*+]\s+(\S.*)$")


def reported(state: RunState) -> bool:
    """Whether a run in `state` gets a report: it ended, or waits for you with its work done."""
    return state in STATUS


def write_report(
    run: Run, run_folder: Path, *, settings: Settings | None, profile: Profile | None
) -> Path:
    """Write `<run folder>/report.md` (atomically) and return its path. Raises ReportError when
    it can't be read or written."""
    path = run_folder / REPORT_FILE
    try:
        text = render_report(run, run_folder, settings=settings, profile=profile)
        write_atomic(path, text)
    except (FactoryError, OSError) as err:
        raise ReportError(f"{path}: couldn't write the run's report ({err}).") from err
    return path


def render_report(
    run: Run, run_folder: Path, *, settings: Settings | None, profile: Profile | None
) -> str:
    """The report's text. `settings` and `profile` give the models; without them they are
    shown as unknown. Raises EventLogError for an event log that can't be read."""
    events = read_events(run_folder)
    review = check_review_file(run_folder / REVIEW_FILE)
    review_found = review.verdict is not None
    findings = review.findings if review_found else ()
    pr = run.pull_request
    lines = [
        f"# Report: run-{run.id}-{run.slug}",
        "",
        f"Status: {STATUS.get(run.state, run.state.replace('_', ' '))}",
        f"Pull request: {pr.url if pr is not None else 'none'}",
        "",
        "## Summary",
        "",
        _line(_summary(run, run_folder, events, len(findings))),
        "",
        "## Acceptance criteria",
        "",
        *_criteria(run_folder),
        "",
        "## Tests",
        "",
        *_tests(run_folder, events, settings),
        "",
        "## Review",
        "",
        f"- Verdict: {review.verdict if review.verdict is not None else 'none (no review)'}",
        *_findings(findings),
        "",
        "## Run",
        "",
        *_run_lines(run, settings, profile),
        "",
        "## Follow-ups",
        "",
        *_follow_ups(run_folder),
    ]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ sections


def _summary(run: Run, folder: Path, events: Sequence[Event], open_findings: int) -> str:
    asked = _request(folder)
    sentences = [f"Requested: {asked}" if asked else "The request is in intent.md."]
    touched = _bullets(_section(_read(folder / HANDOFF_BUILDER), "Files touched"))
    builds = run.attempts.get("builder", 0)
    if builds:
        sentences.append(
            f"The builder touched {_count(len(touched), 'file', 'files')} in "
            f"{_count(builds, 'attempt', 'attempts')} (see {HANDOFF_BUILDER})."
        )
    else:
        sentences.append("Nothing was built.")
    findings = _count(open_findings, "open finding", "open findings")
    if run.state == "needs_you":
        reasons = [e.body.reason for e in events if isinstance(e.body, NeedsYou)]
        why = reasons[-1] if reasons else "see the run's events."
        sentences.append(f"It needs you: {why}")
    elif run.state == "awaiting_merge":
        sentences.append(
            f"Before merging, read the {findings} below and the pull request."
            if open_findings
            else "Nothing is flagged; read the pull request before merging."
        )
    elif run.state == "merged":
        head = run.pull_request.head[:12] if run.pull_request is not None else "its head"
        sentences.append(f"Merged at {head}, with {findings}.")
    else:
        where = f" in the {run.stage} stage" if run.stage else ""
        sentences.append(f"The run ended {run.state}{where}.")
    return " ".join(_sentence(s) for s in sentences)


def _criteria(folder: Path) -> list[str]:
    plan = check_plan(folder / PLAN_FILE)
    if not plan.criteria:
        return ["None: plan.md has no acceptance criteria."]
    states = _judged(folder / REVIEW_FILE)
    tests = _tests_per_criterion(_read(folder / PLAN_FILE))
    lines = []
    for criterion in plan.criteria:
        state = states.get(criterion.number, "not reviewed")
        names = tests.get(criterion.number)
        named = ", ".join(f"`{name}`" for name in names) if names else "no test named"
        lines.append(_line(f"- AC{criterion.number}: {state}, {named}"))
    return _capped(lines, MAX_CRITERIA, "criteria", PLAN_FILE)


def _tests(folder: Path, events: Sequence[Event], settings: Settings | None) -> list[str]:
    runs = [e.body for e in events if isinstance(e.body, TestRun)]
    try:
        cases = load_tester_cases(folder)
    except FactoryError:
        cases = None
    added = "an unknown number" if cases is None else str(len(cases))
    if not runs:
        command = settings.commands.test if settings is not None else None
        shown = _code(command) if command else "none"
        return [_line(f"- Command: {shown}"), "- Result: not run"]
    last = runs[-1]
    result = "passed" if last.exit_code == 0 else "failed"
    passed = "?" if last.passed is None else str(last.passed)
    failed = "?" if last.failed is None else str(last.failed)
    return [
        _line(f"- Command: {_code(last.command)}"),
        f"- Result: {result}, {passed} passed, {failed} failed, {added} added by the tester",
    ]


def _findings(findings: Sequence[Finding]) -> list[str]:
    if not findings:
        return ["- Open findings: none"]
    lines = [
        _line(f"  - [{f.severity}] `{f.path}:{f.line}`: {f.problem}. Fix: {f.fix}")
        for f in findings
    ]
    shown = _capped(lines, MAX_FINDINGS - 1, "findings", REVIEW_FILE, "  ")
    return [f"- Open findings: {len(findings)}", *shown]


def _run_lines(run: Run, settings: Settings | None, profile: Profile | None) -> list[str]:
    metrics = run.metrics
    if settings is not None and profile is not None:
        models = ", ".join(
            f"{role} {resolve_model(profile, getattr(settings.roles, role).model)}"
            for role in ROLES
        )
    else:
        models = "unknown"
    per_attempt = [
        f"{role} {n}: {count}" for (role, n), count in tokens_per_attempt(metrics).items()
    ]
    if len(per_attempt) > MAX_ATTEMPTS_SHOWN:
        rest = len(per_attempt) - MAX_ATTEMPTS_SHOWN
        per_attempt = [*per_attempt[:MAX_ATTEMPTS_SHOWN], f"and {rest} more in run.json"]
    first = {True: "yes", False: "no", None: "not decided"}[metrics.first_pass]
    changed = metrics.pr_lines_changed_by_you
    return [
        _line(f"- Provider: {run.provider}; models: {models}"),
        f"- Isolation: {run.isolation}",
        _line(
            f"- Builder attempts: {run.attempts.get('builder', 0)}; tokens: "
            f"{describe_tokens(metrics)}"
        ),
        _line(f"- Tokens per attempt: {', '.join(per_attempt) if per_attempt else 'none'}"),
        f"- First pass: {first}; builder retries: {metrics.builder_retries}; Important findings: "
        f"{metrics.important_findings}; usage limit pauses: {usage_limit_hits(metrics)}; lines "
        f"you changed on the pull request: {'not measured' if changed is None else changed}",
        f"- Duration: {run.created_at:%Y-%m-%d %H:%M %Z} to {run.updated_at:%Y-%m-%d %H:%M %Z}",
    ]


def _follow_ups(folder: Path) -> list[str]:
    gaps = [
        gap
        for gap in _bullets(_section(_read(folder / HANDOFF_BUILDER), "Gaps"))
        if gap.strip().rstrip(".").lower() != "none"
    ]
    if not gaps:
        return ["None"]
    return _capped([_line(f"- {gap}") for gap in gaps], MAX_FOLLOW_UPS, "gaps", HANDOFF_BUILDER)


# ------------------------------------------------------------------ reading the run's files


def _read(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _request(folder: Path) -> str:
    """The first line of text under `intent.md`'s first `## Request` line."""
    lines = _read(folder / INTENT_FILE).splitlines()
    if REQUEST_HEADING not in lines:
        return ""
    for raw in lines[lines.index(REQUEST_HEADING) + 1 :]:
        text = raw.strip().lstrip("#").strip()
        if text and not text.startswith(("```", "~~~")):
            return _one_line(text)[:160]
    return ""


def _section(text: str, heading: str) -> list[str]:
    """The lines under `## heading` (first occurrence), up to the next `## ` line."""
    out: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("## "):
            if inside:
                break
            inside = line[3:].strip() == heading
        elif inside:
            out.append(line)
    return out


def _bullets(lines: Sequence[str]) -> list[str]:
    return [m.group(1) for line in lines if (m := _BULLET.match(line)) is not None]


def _judged(path: Path) -> dict[int, str]:
    """Each criterion's state in `review.md`: met or not met."""
    states: dict[int, str] = {}
    for line in _section(_read(path), "Acceptance criteria"):
        match = _JUDGED.match(line.strip())
        if match is not None:
            states.setdefault(int(match.group(1)), match.group(2))
    return states


def _tests_per_criterion(plan: str) -> dict[int, list[str]]:
    """The test names the plan's Test cases give each criterion: `- AC<n>: \\`<test>\\`: ...`."""
    tests: dict[int, list[str]] = {}
    for line in _section(plan, "Test cases"):
        match = _CASE.match(line)
        if match is None:
            continue
        name = _NAME.match(match.group(2))
        if name is not None:
            tests.setdefault(int(match.group(1)), []).append(_one_line(name.group(1)))
    return tests


# ------------------------------------------------------------------ text


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _code(text: str) -> str:
    """`text` as inline code on one line (a backtick inside would end it)."""
    return f"`{_one_line(text).replace('`', chr(39))}`"


def _line(text: str) -> str:
    """One report line: no line breaks, at most `MAX_LINE` characters."""
    flat = " ".join(text.split())
    return flat if len(flat) <= MAX_LINE else flat[: MAX_LINE - 1].rstrip() + "…"


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?", "…")) else f"{text}."


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _capped(lines: list[str], cap: int, what: str, source: str, indent: str = "") -> list[str]:
    """At most `cap` lines; the last says how many more are in `source`."""
    if len(lines) <= cap:
        return lines
    kept = lines[: cap - 1]
    return [*kept, f"{indent}- … and {len(lines) - len(kept)} more {what} in {source}"]
