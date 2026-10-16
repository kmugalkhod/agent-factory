"""The reviewer gate (design: gates between stages; `templates/README.md`, review.md).

The reviewer writes `review.md` only, and it serves as its handoff. The gate reads it and decides:

- pass: no finding is Important and the verdict is `approve`;
- fail, the builder resumes with `review.md`: one or more Important findings, whose count goes
  into the run's metrics (`record_findings`);
- fail, the reviewer retries: the file is missing, doesn't follow the template's headings and
  line formats, or contradicts itself (an `approve` verdict next to an Important finding or a
  criterion not met, or `request changes` with no Important finding to act on).

The gate is a pure function of the file's text. That the reviewer's stage leaves the worktree
unchanged is the post-stage check's job, through the reviewer's write paths.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from factory_engine.events import GateResult
from factory_engine.run import Run

GATE = "review"
HEADINGS = ("Acceptance criteria", "Findings", "Verdict")
FINDING_FORMAT = "- [<Important | Minor | Nit>] `<path>:<line>`: <problem>. Fix: <what to change>"
VERDICT_FORMAT = "`Verdict: approve` or `Verdict: request changes`"
CRITERION_FORMAT = "`- AC<n>: <met | not met>. <evidence>`"
MAX_QUOTED = 120  # characters of a bad line quoted in the reason

Severity = Literal["Important", "Minor", "Nit"]
Verdict = Literal["approve", "request changes"]
Retry = Literal["reviewer", "builder"]

_FINDING = re.compile(
    r"- \[(?P<severity>Important|Minor|Nit)\] `(?P<path>[^`]+):(?P<line>[^`:]+)`: "
    r"(?P<problem>.+?)\. Fix: (?P<fix>.+)"
)
_VERDICT = re.compile(r"Verdict: (?P<verdict>approve|request changes)")
_LABEL = re.compile(r"- AC(?P<number>\d+): ")  # a line that names a criterion
# The whole status, the template's full stop, then evidence; "met | not met. ..." is unclear.
_JUDGED = re.compile(r"- AC\d+: (?P<status>met|not met)\.\s+(?P<evidence>\S.*)")

type Body = list[tuple[int, str]]  # (line number, stripped text) of non-blank lines


@dataclass(frozen=True)
class Finding:
    severity: Severity
    path: str
    line: str
    problem: str
    fix: str


@dataclass(frozen=True)
class ReviewOutcome:
    """What the gate decided. On a fail, `retry` names who goes next: the reviewer when
    `review.md` is malformed, the builder when it has Important findings."""

    passed: bool
    reason: str
    retry: Retry | None = None
    important: int = 0  # Important findings; 0 when the file is malformed
    findings: tuple[Finding, ...] = ()
    verdict: Verdict | None = None

    def event(self) -> GateResult:
        return GateResult(gate=GATE, passed=self.passed, reason=self.reason)


def check_review_file(path: Path) -> ReviewOutcome:
    """Check the `review.md` at `path`. A missing or unreadable file is malformed."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        return ReviewOutcome(
            passed=False,
            reason=f"review.md can't be read ({err}). Write it from the review template.",
            retry="reviewer",
        )
    return check_review(text)


def check_review(text: str) -> ReviewOutcome:
    """Check the text of a `review.md`."""
    headings, bodies = _sections(text.removeprefix("\ufeff").splitlines())
    if tuple(headings) != HEADINGS:
        found = _listed(headings) if headings else "none"
        return _malformed(
            f"its `## ` headings must be exactly {_listed(HEADINGS)}, in that order; found {found}"
        )

    problems: list[str] = []
    unmet = _unmet_criteria(bodies["Acceptance criteria"], problems)
    findings = _findings(bodies["Findings"], problems)
    verdict = _verdict(bodies["Verdict"], problems)
    if problems or verdict is None:
        return _malformed(*problems)

    important = sum(1 for finding in findings if finding.severity == "Important")
    if verdict == "approve" and important:
        problems.append(
            f"the verdict is approve but {_plural(important, 'finding is', 'findings are')} "
            "Important; request changes or change the severity"
        )
    if verdict == "approve" and unmet:
        problems.append(
            f"the verdict is approve but {_plural(unmet, 'criterion is', 'criteria are')} "
            "not met; request changes with an Important finding for each"
        )
    if verdict == "request changes" and not important:
        problems.append(
            "the verdict is request changes but no finding is Important; add an Important "
            "finding for each blocking problem (a criterion not met is one) or approve"
        )
    if problems:
        return _malformed(*problems)

    if important:
        return ReviewOutcome(
            passed=False,
            reason=f"{_plural(important, 'Important finding', 'Important findings')}; "
            "the builder resumes with review.md",
            retry="builder",
            important=important,
            findings=findings,
            verdict=verdict,
        )
    others = [
        _plural(count, severity, severity)
        for severity in ("Minor", "Nit")
        if (count := sum(1 for finding in findings if finding.severity == severity))
    ]
    reason = "no Important findings" + (f" ({', '.join(others)})" if others else "")
    return ReviewOutcome(passed=True, reason=reason, findings=findings, verdict=verdict)


def parse_finding(line: str) -> Finding | None:
    """One finding line in the template's format, or None if the line isn't one."""
    match = _FINDING.fullmatch(line.strip())
    if match is None:
        return None
    return Finding(
        severity=cast(Severity, match["severity"]),
        path=match["path"],
        line=match["line"],
        problem=match["problem"],
        fix=match["fix"],
    )


def record_findings(run: Run, outcome: ReviewOutcome) -> Run:
    """A copy of `run` with the review's Important findings added to its metrics."""
    total = run.metrics.important_findings + outcome.important
    metrics = run.metrics.model_copy(update={"important_findings": total})
    return run.model_copy(deep=True, update={"metrics": metrics})


# ------------------------------------------------------------------ parsing


def _sections(lines: Sequence[str]) -> tuple[list[str], dict[str, Body]]:
    """The `## ` headings in order, and the non-blank lines under each (first occurrence).
    Lines before the first heading (the title, attempt and diff) aren't part of any section."""
    headings: list[str] = []
    bodies: dict[str, Body] = {}
    body: Body | None = None
    for number, line in enumerate(lines, start=1):
        if line.startswith("## "):
            heading = line[3:].strip()
            headings.append(heading)
            body = bodies.setdefault(heading, [])
        elif body is not None and line.strip():
            body.append((number, line.strip()))
    return headings, bodies


def _unmet_criteria(body: Body, problems: list[str]) -> int:
    """How many criteria are `not met`. Every line of the section must be a criterion in
    `CRITERION_FORMAT`, each criterion once, and at least one must exist: a line in any other
    form could hide a criterion not met."""
    seen: set[int] = set()
    unmet = 0
    for number, line in body:
        label = _LABEL.match(line)
        if label is None:
            problems.append(
                f"line {number} under Acceptance criteria isn't a criterion in the form "
                f"{CRITERION_FORMAT}: {_quote(line)}"
            )
            continue
        criterion = int(label["number"])
        if criterion in seen:
            problems.append(f"line {number} repeats AC{criterion}; mark each criterion once")
        seen.add(criterion)
        judged = _JUDGED.fullmatch(line)
        if judged is None:
            problems.append(
                f"line {number} names a criterion but doesn't say `met` or `not met`, a full "
                f"stop, then its evidence ({CRITERION_FORMAT}): {_quote(line)}"
            )
        elif judged["status"] == "not met":
            unmet += 1
    if not seen:
        problems.append(
            f"the Acceptance criteria section marks no criterion; write one {CRITERION_FORMAT} "
            "line for each"
        )
    return unmet


def _findings(body: Body, problems: list[str]) -> tuple[Finding, ...]:
    if not body:
        problems.append('the Findings section is empty; write "None" or one line per finding')
        return ()
    if len(body) == 1 and _is_none(body[0][1]):
        return ()
    findings: list[Finding] = []
    for number, line in body:
        if _is_none(line):
            problems.append(f'line {number} says "None" next to findings; keep one or the other')
        elif (finding := parse_finding(line)) is None:
            problems.append(
                f"line {number} under Findings isn't a finding in the form {FINDING_FORMAT}: "
                f"{_quote(line)}"
            )
        else:
            findings.append(finding)
    return tuple(findings)


def _verdict(body: Body, problems: list[str]) -> Verdict | None:
    if len(body) == 1 and (match := _VERDICT.fullmatch(body[0][1])):
        return cast(Verdict, match["verdict"])
    found = ", ".join(_quote(line) for _, line in body[:3]) if body else "nothing"
    problems.append(
        f"the Verdict section must hold exactly one line, {VERDICT_FORMAT}; found {found}"
    )
    return None


# ------------------------------------------------------------------ helpers


def _malformed(*problems: str) -> ReviewOutcome:
    reason = f"review.md is malformed: {'; '.join(problems)}. Rewrite it from the review template."
    return ReviewOutcome(passed=False, reason=reason, retry="reviewer")


def _is_none(line: str) -> bool:
    return line.rstrip(".").strip().lower() == "none"


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _listed(names: Sequence[str]) -> str:
    return ", ".join(f'"{name}"' for name in names)


def _quote(line: str) -> str:
    cut = line if len(line) <= MAX_QUOTED else line[: MAX_QUOTED - 3] + "..."
    return f'"{cut}"'
