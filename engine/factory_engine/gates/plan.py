"""The planner gate (design: run flow, gates between stages).

After the planner, the engine reads `plan.md` by the headings fixed in `templates/README.md`.
The plan passes only when it names files, steps and at least one measurable acceptance
criterion; otherwise the planner is retried with the reason. The gate also returns the Split
and Questions sections as data, for the split approval and the planner questions.

Rules, checked line by line (no Markdown parser, so the result never depends on a library):

- Headings: every required `## ` heading once, optional ones at most once, no others, in the
  template's order. Text before the first heading (the `# ` title) is ignored.
- No code fences: gates read headings line by line, so a fenced `## ` line would be misread.
- An item is a list line at column 0: `1.` or `1)` for a numbered one, `-`, `*` or `+` for a
  bullet. A non-blank line right after it continues it, and so does an indented line after a
  blank one; anything else ends it. An item that is only a template placeholder (`<step>`)
  doesn't count.
- Files: list items that start with a path in backticks (``- `src/app.py`: change, why``).
- Steps and acceptance criteria: numbered items. Bullets don't count.
- A criterion is measurable when its text has a code span in backticks (a command, call, file
  or value), a digit (a value), a test name (`test_...`) or one of `OBSERVABLE` (an observable
  result, such as returns, raises, exits, prints, writes or shows). This is a floor, not a
  judgement: you still check the criteria when you approve the plan.
- Split (optional): numbered parts 1, 2, 3, ... in order, at least two, each
  `<title>: <scope>, depends on <none | part N>`. A part may depend only on earlier parts.
- Questions (optional): numbered; `Why it matters:` and `Default if unanswered:` are read
  when present.

Every problem is reported at once, so one retry can fix them all.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from factory_engine.events import GateResult

GATE = "plan"
PLAN_FILE = "plan.md"

REQUIRED = ("Context", "Past runs used", "Files", "Steps", "Acceptance criteria", "Test cases")
OPTIONAL = ("Split", "Questions")
SECTIONS = REQUIRED + OPTIONAL

# Words naming a result someone can observe. Only verb forms that are rarely nouns, so "the
# output is clean" or "logs are tidy" stay vague.
OBSERVABLE = (
    "returns", "returned", "raises", "raised", "throws", "thrown", "exits", "exited",
    "prints", "printed", "writes", "wrote", "written", "creates", "created", "deletes",
    "deleted", "removes", "removed", "contains", "shows", "showed", "shown", "displays",
    "displayed", "renders", "rendered", "emits", "emitted", "logged", "responds",
    "responded", "rejects", "rejected", "refuses", "refused", "fails", "failed", "passes",
    "passed", "equals", "matches", "matched", "exists", "stores", "stored", "saves", "saved",
    "sends", "sent", "redirects", "redirected", "appears", "appeared",
)  # fmt: skip

_NUMBERED = re.compile(r"^(\d+)[.)]\s+(\S.*)$")
_BULLET = re.compile(r"^[-*+]\s+(\S.*)$")
_PLACEHOLDER = re.compile(r"^<[^<>]*>$")
_FILE = re.compile(r"^`([^`]+)`")
_FENCES = ("```", "~~~")
_CODE = re.compile(r"`[^`]+`")
_DIGIT = re.compile(r"\d")
_TEST = re.compile(r"\btest_\w+")
_VERB = re.compile(r"\b(?:" + "|".join(OBSERVABLE) + r")\b", re.IGNORECASE)
_DEPENDS = re.compile(r"^(?P<head>.*)\bdepends on\b(?P<deps>.*)$", re.IGNORECASE)
_WHY = re.compile(r"\bwhy it matters:", re.IGNORECASE)
_DEFAULT = re.compile(r"\bdefault if unanswered:", re.IGNORECASE)


class ProblemKind(StrEnum):
    MISSING_PLAN = "missing_plan"
    HEADINGS = "headings"
    CODE_FENCE = "code_fence"
    NO_FILES = "no_files"
    NO_STEPS = "no_steps"
    NO_CRITERIA = "no_criteria"
    VAGUE_CRITERIA = "vague_criteria"
    SPLIT = "split"
    QUESTIONS = "questions"


@dataclass(frozen=True)
class Problem:
    kind: ProblemKind
    message: str  # one line: what is wrong and what to do


@dataclass(frozen=True)
class Criterion:
    number: int
    text: str
    measurable: bool


@dataclass(frozen=True)
class SplitPart:
    number: int
    title: str
    scope: str
    depends_on: tuple[int, ...]


@dataclass(frozen=True)
class Question:
    number: int
    text: str
    why: str | None
    default: str | None


@dataclass(frozen=True)
class PlanCheck:
    """The gate's verdict and what it read. `split` and `questions` are None when the plan has
    no such section; they are filled even when the plan fails."""

    problems: tuple[Problem, ...]
    files: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()
    criteria: tuple[Criterion, ...] = ()
    split: tuple[SplitPart, ...] | None = None
    questions: tuple[Question, ...] | None = None

    @property
    def passed(self) -> bool:
        return not self.problems

    @property
    def reason(self) -> str:
        """On fail, one line per problem, for the planner retry. On pass, what the plan holds."""
        if self.problems:
            return "\n".join(problem.message for problem in self.problems)
        vague = [str(c.number) for c in self.criteria if not c.measurable]
        measured = len(self.criteria) - len(vague)
        quality = (
            "all measurable"
            if not vague
            else f"{measured} measurable (not measurable: {', '.join(vague)})"
        )
        reason = (
            f"{PLAN_FILE} names {_count(len(self.files), 'file', 'files')}, "
            f"{_count(len(self.steps), 'step', 'steps')} and "
            f"{_count(len(self.criteria), 'acceptance criterion', 'acceptance criteria')}, "
            f"{quality}."
        )
        extras: list[str] = []
        if self.split:
            extras.append(f"proposes a split into {_count(len(self.split), 'part', 'parts')}")
        if self.questions:
            extras.append(f"asks {_count(len(self.questions), 'question', 'questions')}")
        if extras:
            reason += f" It {' and '.join(extras)}."
        return reason

    def event(self) -> GateResult:
        return GateResult(gate=GATE, passed=self.passed, reason=self.reason)


def check_plan(path: Path) -> PlanCheck:
    """Check the `plan.md` at `path`. A missing or unreadable file fails the gate."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return _missing(
            f"{path} doesn't exist. Write {PLAN_FILE} in the run folder from the plan template."
        )
    except OSError as err:
        return _missing(
            f"Can't read {path} ({err}). Write {PLAN_FILE} as a file in the run folder."
        )
    return check_plan_text(data.decode("utf-8", errors="replace"))


def check_plan_text(text: str) -> PlanCheck:
    """Check the text of a `plan.md`."""
    lines = text.removeprefix("﻿").splitlines()
    headings, bodies = _sections(lines)
    problems = _check_headings(headings)
    problems += _check_fences(lines)

    files: tuple[str, ...] = ()
    if "Files" in bodies:
        files = _files(bodies["Files"])
        if not files:
            problems.append(Problem(ProblemKind.NO_FILES, _NO_FILES))

    steps: tuple[str, ...] = ()
    if "Steps" in bodies:
        steps = tuple(item.text for item in _numbered(bodies["Steps"]))
        if not steps:
            problems.append(Problem(ProblemKind.NO_STEPS, _NO_STEPS))

    criteria: tuple[Criterion, ...] = ()
    if "Acceptance criteria" in bodies:
        criteria = tuple(
            Criterion(number=item.number, text=item.text, measurable=is_measurable(item.text))
            for item in _numbered(bodies["Acceptance criteria"])
        )
        if not criteria:
            problems.append(Problem(ProblemKind.NO_CRITERIA, _NO_CRITERIA))
        elif not any(c.measurable for c in criteria):
            numbers = ", ".join(str(c.number) for c in criteria)
            problems.append(Problem(ProblemKind.VAGUE_CRITERIA, _VAGUE.format(numbers=numbers)))

    split: tuple[SplitPart, ...] | None = None
    if "Split" in bodies:
        split, split_problems = _split(bodies["Split"])
        problems += split_problems

    questions: tuple[Question, ...] | None = None
    if "Questions" in bodies:
        questions = tuple(_question(item) for item in _numbered(bodies["Questions"]))
        if not questions:
            problems.append(Problem(ProblemKind.QUESTIONS, _NO_QUESTIONS))

    return PlanCheck(
        problems=tuple(problems),
        files=files,
        steps=steps,
        criteria=criteria,
        split=split,
        questions=questions,
    )


def is_measurable(text: str) -> bool:
    """Whether a criterion names a command, value, test or observable result (module rules)."""
    return any(rule.search(text) for rule in (_CODE, _DIGIT, _TEST, _VERB))


# ------------------------------------------------------------------ messages

_TEMPLATE_ORDER = ", ".join(f"`## {name}`" for name in SECTIONS)
_NO_FILES = (
    "`## Files` names no files. List every file to create or change, one per line, as "
    "``- `<path>`: <create | change>, <why>``, with the path in backticks."
)
_NO_STEPS = (
    "`## Steps` has no numbered steps. Write the builder's steps as a numbered list "
    "(`1.`, `2.`, ...), each small enough to check on its own."
)
_NO_CRITERIA = (
    "`## Acceptance criteria` has no numbered criteria (bullets don't count). Write each "
    "criterion as a numbered line (`1.`, `2.`, ...) that the tester can turn into a test."
)
_VAGUE = (
    "No acceptance criterion is measurable (criteria {numbers}). Name a command or value in "
    "backticks, a number, a test, or an observable result such as returns, raises, exits, "
    "prints, writes or shows, for example: `factory status --json` exits 0 and prints `[]`."
)
_NO_PARTS = (
    "`## Split` has no numbered parts. List the parts in order as "
    "`1. <part title>: <scope>, depends on <none | part N>`, or delete the section when the "
    "change needs no split."
)
_NO_QUESTIONS = (
    "`## Questions` has no numbered questions. Number each question (`1.`, `2.`, ...), or "
    "delete the section when you have none."
)


def _missing(message: str) -> PlanCheck:
    return PlanCheck(problems=(Problem(ProblemKind.MISSING_PLAN, message),))


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


# ------------------------------------------------------------------ headings and sections


def _sections(lines: Sequence[str]) -> tuple[list[tuple[str, int]], dict[str, list[str]]]:
    """The `## ` headings with their line numbers, and each heading's body lines (the bodies
    of a repeated heading are joined)."""
    headings: list[tuple[str, int]] = []
    bodies: dict[str, list[str]] = {}
    current: list[str] | None = None
    for number, line in enumerate(lines, start=1):
        if line.startswith("## "):
            name = line[3:].strip()
            headings.append((name, number))
            current = bodies.setdefault(name, [])
        elif current is not None:
            current.append(line)
    return headings, bodies


def _check_headings(headings: Sequence[tuple[str, int]]) -> list[Problem]:
    problems: list[Problem] = []
    names = [name for name, _ in headings]
    for name, line in headings:
        if name not in SECTIONS:
            problems.append(
                Problem(
                    ProblemKind.HEADINGS,
                    f"Line {line} has the heading `## {name}`, which the plan template doesn't "
                    f"have. Use only the template's headings: {_TEMPLATE_ORDER}.",
                )
            )
    missing = [f"`## {name}`" for name in REQUIRED if name not in names]
    if missing:
        problems.append(
            Problem(
                ProblemKind.HEADINGS,
                f"{PLAN_FILE} is missing {', '.join(missing)}. Keep every required heading, "
                "even when its section is None.",
            )
        )
    for name in SECTIONS:
        count = names.count(name)
        if count > 1:
            problems.append(
                Problem(
                    ProblemKind.HEADINGS,
                    f"`## {name}` appears {count} times in {PLAN_FILE}; keep one.",
                )
            )
    known = [name for name in dict.fromkeys(names) if name in SECTIONS]
    if known != sorted(known, key=SECTIONS.index):
        problems.append(
            Problem(
                ProblemKind.HEADINGS,
                f"The headings in {PLAN_FILE} are out of order. Use the template's order: "
                f"{_TEMPLATE_ORDER}.",
            )
        )
    return problems


def _check_fences(lines: Sequence[str]) -> list[Problem]:
    found = [str(n) for n, line in enumerate(lines, start=1) if line.lstrip().startswith(_FENCES)]
    if not found:
        return []
    return [
        Problem(
            ProblemKind.CODE_FENCE,
            f"{PLAN_FILE} has a code fence (line {', '.join(found)}). Gates read headings line "
            "by line, so remove the fences and put commands in `backticks`.",
        )
    ]


# ------------------------------------------------------------------ list items


@dataclass(frozen=True)
class _Item:
    number: int | None  # None for a bullet
    text: str


def _items(lines: Sequence[str]) -> list[_Item]:
    """The list items in a section's lines, placeholders left out (module rules)."""
    found: list[tuple[int | None, list[str]]] = []
    in_item = False
    after_blank = False
    for line in lines:
        if not line.strip():
            after_blank = True
            continue
        numbered = _NUMBERED.match(line)
        bullet = None if numbered else _BULLET.match(line)
        if numbered:
            found.append((int(numbered.group(1)), [numbered.group(2).strip()]))
            in_item = True
        elif bullet:
            found.append((None, [bullet.group(1).strip()]))
            in_item = True
        elif in_item and (not after_blank or line[0].isspace()):
            found[-1][1].append(line.strip())
        else:
            in_item = False
        after_blank = False
    items = [_Item(number, " ".join(parts)) for number, parts in found]
    return [item for item in items if not _PLACEHOLDER.match(item.text)]


@dataclass(frozen=True)
class _NumberedItem:
    number: int
    text: str


def _numbered(lines: Sequence[str]) -> list[_NumberedItem]:
    return [
        _NumberedItem(item.number, item.text) for item in _items(lines) if item.number is not None
    ]


def _files(lines: Sequence[str]) -> tuple[str, ...]:
    paths: list[str] = []
    for item in _items(lines):
        match = _FILE.match(item.text)
        if match is None:
            continue
        path = match.group(1).strip()
        if path and not _PLACEHOLDER.match(path):
            paths.append(path)
    return tuple(paths)


# ------------------------------------------------------------------ Split and Questions


def _split(lines: Sequence[str]) -> tuple[tuple[SplitPart, ...], list[Problem]]:
    items = _numbered(lines)
    if not items:
        return (), [Problem(ProblemKind.SPLIT, _NO_PARTS)]
    problems: list[Problem] = []
    if len(items) < 2:
        problems.append(
            Problem(
                ProblemKind.SPLIT,
                "A split needs at least two parts. Delete `## Split` when one merge request "
                "is enough.",
            )
        )
    numbers = [item.number for item in items]
    if numbers != list(range(1, len(items) + 1)):
        problems.append(
            Problem(
                ProblemKind.SPLIT,
                f"Split parts must be numbered 1, 2, 3, ... in order; found "
                f"{', '.join(str(n) for n in numbers)}.",
            )
        )
    parts: list[SplitPart] = []
    for index, item in enumerate(items):
        part, problem = _part(item, earlier=numbers[:index])
        parts.append(part)
        if problem is not None:
            problems.append(problem)
    return tuple(parts), problems


def _part(item: _NumberedItem, earlier: Sequence[int]) -> tuple[SplitPart, Problem | None]:
    """One split part, and the problem with its dependencies, if any."""
    match = _DEPENDS.match(item.text)
    head = match.group("head") if match else item.text
    title, _, scope = head.rstrip(" ,;.").partition(": ")
    depends_on: tuple[int, ...] = ()
    problem: Problem | None = None
    if match is None:
        problem = Problem(
            ProblemKind.SPLIT,
            f"Split part {item.number} doesn't say what it depends on. End it with "
            "`depends on none` or `depends on part N`.",
        )
    else:
        clause = match.group("deps").strip().rstrip(".").strip()
        if clause.lower() != "none":
            depends_on = tuple(int(n) for n in re.findall(r"\d+", clause))
            if not depends_on:
                problem = Problem(
                    ProblemKind.SPLIT,
                    f"Split part {item.number} depends on `{clause}`, which names no part "
                    "number. Write `depends on part N` or `depends on none`.",
                )
            elif bad := [n for n in depends_on if n not in earlier]:
                problem = Problem(
                    ProblemKind.SPLIT,
                    f"Split part {item.number} depends on part {bad[0]}, which isn't an earlier "
                    "part. A part may depend only on parts listed before it.",
                )
    part = SplitPart(
        number=item.number, title=title.strip(), scope=scope.strip(), depends_on=depends_on
    )
    return part, problem


def _question(item: _NumberedItem) -> Question:
    rest = item.text
    default: str | None = None
    if found := _DEFAULT.search(rest):
        default = rest[found.end() :].strip() or None
        rest = rest[: found.start()]
    why: str | None = None
    if found := _WHY.search(rest):
        why = rest[found.end() :].strip() or None
        rest = rest[: found.start()]
    return Question(number=item.number, text=rest.strip(), why=why, default=default)
