"""Secret scan for text and diffs.

The hook runs it on the text a Write, Edit, MultiEdit or NotebookEdit call would put on disk;
the post-stage check runs it on the stage's diff (added lines only: removing a secret is fine).
It looks for:

- Anthropic keys and OAuth tokens (`sk-ant-...`).
- AWS access key IDs (`AKIA...`, `ASIA...`) and secret access keys next to their name.
- GitHub tokens (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_`).
- Private keys (`-----BEGIN ... PRIVATE KEY-----`), one finding per key.
- Assignments copied from `.env` files: an upper-case name that says secret (`*_PASSWORD`,
  `*_TOKEN`, `*_SECRET`, `*_API_KEY`, ...) set to a real-looking value. Empty values,
  placeholders (`<...>`, `${VAR}`, `changeme`, `your-...`), numbers, booleans and references
  to other names are skipped, so `.env.example` files and settings code stay clean.
- Passwords in URLs (`postgres://user:pass@host`).

A finding never carries the secret: only its kind, where it is and a short masked preview.
"""

import codecs
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType

ANTHROPIC = "Anthropic key"
AWS_KEY_ID = "AWS access key ID"
AWS_SECRET_KEY = "AWS secret access key"
GITHUB = "GitHub token"
PRIVATE_KEY = "private key"
ENV = ".env secret"
URL = "password in URL"

# Token formats, most specific first. Group "value" is the secret.
_TOKENS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (ANTHROPIC, re.compile(r"(?<![A-Za-z0-9_-])(?P<value>sk-ant-[a-z0-9]+-[A-Za-z0-9_-]{20,})")),
    (
        AWS_KEY_ID,
        re.compile(
            r"(?<![A-Za-z0-9/+])(?P<value>(?:AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16})(?![A-Za-z0-9/+=])"
        ),
    ),
    (
        AWS_SECRET_KEY,
        re.compile(
            r"(?i)(?:aws_?secret_?access_?key|secret_?access_?key)[\"']?\s*[:=]\s*[\"']?"
            r"(?P<value>[A-Za-z0-9/+]{40})(?![A-Za-z0-9/+=])"
        ),
    ),
    (
        GITHUB,
        re.compile(
            r"(?<![A-Za-z0-9_])(?P<value>gh[pousr]_[A-Za-z0-9]{36,255}"
            r"|github_pat_[A-Za-z0-9_]{22,255})(?![A-Za-z0-9_])"
        ),
    ),
    (
        PRIVATE_KEY,
        re.compile(r"(?P<value>-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----)"),
    ),
)

# `NAME=value` as in a .env file: optional `export`, no space before `=`, any value. A quoted
# value may hold spaces and the other quote; a double-quoted one may hold `\"`.
_ENV_TIGHT = re.compile(
    r"^\s*(?:export\s+)?(?P<name>[A-Z][A-Z0-9_]*)="
    r"(?:\"(?P<double>(?:[^\"\\]|\\.)*)\"|'(?P<single>[^']*)'|(?P<value>[^\s\"']*))"
    r"\s*(?:#.*)?$"
)
_VALUE_GROUPS = ("double", "single", "value")
# `NAME = value` with spaces: only an unquoted value counts. In code a quoted value is far more
# often a header name or a label than a pasted secret.
_ENV_SPACED = re.compile(
    r"^\s*(?:export\s+)?(?P<name>[A-Z][A-Z0-9_]*)\s+=\s*(?P<value>[^\s\"']+)\s*(?:#.*)?$"
)
_SECRET_WORDS = frozenset(
    {
        "SECRET",
        "SECRETS",
        "TOKEN",
        "PASSWORD",
        "PASSWD",
        "PASS",
        "PWD",
        "PASSPHRASE",
        "CREDENTIAL",
        "CREDENTIALS",
        "APIKEY",
    }
)
# `<word>_KEY` names that hold a secret (`SORT_KEY` and `CACHE_KEY` don't).
_KEY_WORDS = frozenset(
    {"API", "ACCESS", "PRIVATE", "SECRET", "SIGNING", "ENCRYPTION", "MASTER", "CLIENT", "SERVICE"}
)
_URL = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s:/@\"'<>]*:(?P<value>[^\s@/\"'<>]+)@")

# A placeholder is a whole-value form, never a fragment found inside a value: `<...>`,
# `{{ ... }}`, a value led by a placeholder word (`your-api-key`, `REPLACE_ME`, `changeme`) and
# a masked value (`sk_live_...`, `ab12****`, `sk_test_xxxx`).
_PLACEHOLDER = re.compile(
    r"<.*>|\{\{.*\}\}"
    r"|(?:your|my|replace|insert|enter|put|change|example|sample|dummy|fake|placeholder|redacted)"
    r"(?:me|[-_. ].*)?"
    r"|.*(?:\.\.\.|…|\*{3,}).*|.*[-_](?:x{4,}|X{4,})",
    re.IGNORECASE,
)
_PLACEHOLDER_WORDS = frozenset(
    {"password", "pass", "secret", "token", "pwd", "true", "false", "null", "none", "yes", "no"}
)
# A reference to another name, not a value: OTHER_NAME, settings.X.
_REFERENCE = re.compile(r"[A-Z]+(?:_[A-Z0-9]+)+|[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+")
_MIN_ENV_VALUE = 8
_MIN_URL_VALUE = 3

_STORE_HINT = (
    "Keep secrets out of the repo: code and tests read them from the environment, and test "
    "secrets live in the repo's `.env.factory` file outside the worktree. Write a placeholder "
    "or an environment lookup instead."
)


@dataclass(frozen=True)
class SecretFinding:
    """One secret found. `preview` is masked; the full value is never kept."""

    kind: str
    line: int  # 1-based, in the scanned text (in the new file for a diff)
    preview: str
    path: str | None = None

    def describe(self) -> str:
        where = f"{self.path}:{self.line}" if self.path else f"line {self.line}"
        return f"{self.kind} at {where} ({self.preview})"


@dataclass(frozen=True)
class SecretDecision:
    allowed: bool
    reason: str = ""


ALLOW = SecretDecision(True)


def scan_text(text: str, *, path: str | None = None) -> list[SecretFinding]:
    """Every secret in `text`, in order. `path` is only used to label the findings."""
    findings: list[SecretFinding] = []
    for number, line in enumerate(_lines(text), start=1):
        findings.extend(_scan_line(line, number, path))
    return findings


def scan_diff(diff: str) -> list[SecretFinding]:
    """Every secret on an added line of a unified diff (`git diff` output), labelled with the
    file and its line number in the new version."""
    findings: list[SecretFinding] = []
    for path, number, line in _added_lines(diff):
        findings.extend(_scan_line(line, number, path))
    return findings


# Tool -> the input field holding the new text. MultiEdit holds it in each of its `edits`.
CONTENT_TOOLS: Mapping[str, str] = MappingProxyType(
    {"Write": "content", "Edit": "new_string", "NotebookEdit": "new_source"}
)


def check_tool_content(tool_name: str, tool_input: Mapping[str, object]) -> SecretDecision:
    """Block a file-tool call whose new text holds a secret. A tool that writes no text (Read,
    Glob, a notebook cell delete) is allowed; a write whose text can't be read is blocked."""
    texts = _new_texts(tool_name, tool_input)
    if isinstance(texts, SecretDecision):
        return texts
    raw_path = tool_input.get("notebook_path" if tool_name == "NotebookEdit" else "file_path")
    path = raw_path if isinstance(raw_path, str) and raw_path.strip() else tool_name
    label = path if tool_name == "Write" else f"{path} (new text)"
    for text in texts:
        findings = scan_text(text, path=label)
        if findings:
            listed = "; ".join(finding.describe() for finding in findings)
            return _block(f"{tool_name} would write a secret: {listed}. {_STORE_HINT}")
    return ALLOW


def _new_texts(tool_name: str, tool_input: Mapping[str, object]) -> list[str] | SecretDecision:
    if tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        if not isinstance(edits, list):
            return _block("MultiEdit needs a list of edits in `edits`.")
        texts: list[str] = []
        for edit in edits:
            text = edit.get("new_string") if isinstance(edit, dict) else None
            if not isinstance(text, str):
                return _block("every MultiEdit edit needs its new text in `new_string`.")
            texts.append(text)
        return texts
    field = CONTENT_TOOLS.get(tool_name)
    if field is None:
        return []
    text = tool_input.get(field)
    if text is None and tool_name == "NotebookEdit" and tool_input.get("edit_mode") == "delete":
        return []
    if not isinstance(text, str):
        return _block(f"{tool_name} needs its new text in `{field}`.")
    return [text]


def _block(reason: str) -> SecretDecision:
    return SecretDecision(False, f"Blocked: {reason}")


# ------------------------------------------------------------------ one line


def _scan_line(line: str, number: int, path: str | None) -> list[SecretFinding]:
    found: list[tuple[int, int, SecretFinding]] = []

    def add(kind: str, start: int, end: int, preview: str) -> None:
        if any(start < other_end and other_start < end for other_start, other_end, _ in found):
            return  # already reported by a more specific rule
        found.append((start, end, SecretFinding(kind, number, preview, path)))

    for kind, pattern in _TOKENS:
        for match in pattern.finditer(line):
            value = match.group("value")
            preview = value if kind == PRIVATE_KEY else _mask(value)
            add(kind, match.start("value"), match.end("value"), preview)
    for name, value, start in _env_assignments(line):
        add(ENV, start, start + len(value), f"{name}={_mask(value)}")
    for match in _URL.finditer(line):
        value = match.group("value")
        if len(value) >= _MIN_URL_VALUE and not _placeholder(value):
            add(URL, match.start("value"), match.end("value"), f"…:{_mask(value)}@…")
    found.sort(key=lambda item: item[0])
    return [finding for _, _, finding in found]


def _env_assignments(line: str) -> Iterator[tuple[str, str, int]]:
    """(name, value, where the value starts) for a secret assignment on the line."""
    match = _ENV_TIGHT.match(line) or _ENV_SPACED.match(line)
    if match is None:
        return
    group = next(
        g for g in _VALUE_GROUPS if g in match.re.groupindex and match.group(g) is not None
    )
    name, value = match.group("name"), match.group(group)
    if not _secret_name(name) or len(value) < _MIN_ENV_VALUE or _placeholder(value):
        return
    if value.isdigit() or _REFERENCE.fullmatch(value):
        return
    yield name, value, match.start(group)


def _secret_name(name: str) -> bool:
    words = name.split("_")
    if _SECRET_WORDS.intersection(words):
        return True
    return any(
        word == "KEY" and i > 0 and words[i - 1] in _KEY_WORDS for i, word in enumerate(words)
    )


def _placeholder(value: str) -> bool:
    lowered = value.lower()
    if lowered in _PLACEHOLDER_WORDS or value[0] in "$%" or len(set(value)) == 1:
        return True
    return _PLACEHOLDER.fullmatch(value) is not None


def _lines(text: str) -> list[str]:
    """Split on `\\n` only, as git and editors do. `str.splitlines` also splits on form feeds and
    Unicode separators inside a line, which shifts diff hunk counts and cuts values apart."""
    return [line.removesuffix("\r") for line in text.split("\n")]


def _mask(value: str) -> str:
    """At most four leading characters, and never more than a quarter of the value."""
    return value[: min(4, len(value) // 4)] + "…"


# ------------------------------------------------------------------ diffs


_HUNK = re.compile(r"^@@ -\d+(?:,(?P<old>\d+))? \+(?P<new>\d+)(?:,(?P<count>\d+))? @@")


def _added_lines(diff: str) -> Iterator[tuple[str | None, int, str]]:
    """(path, new line number, text) for each added line. The hunk header's counts say where a
    hunk ends, so an added line that starts with `++` is never taken for a file header."""
    path: str | None = None
    old_left = new_left = 0
    number = 0
    for line in _lines(diff):
        if old_left > 0 or new_left > 0:
            mark, text = line[:1], line[1:]
            if mark == "+":
                yield path, number, text
                number += 1
                new_left -= 1
            elif mark == "-":
                old_left -= 1
            elif mark == "\\":
                pass  # "\ No newline at end of file"
            else:  # context; some tools strip the leading space of an empty context line
                number += 1
                old_left -= 1
                new_left -= 1
            continue
        if line.startswith("diff --git "):
            path = None
        elif line.startswith("+++ "):
            path = _diff_path(line[4:])
        elif hunk := _HUNK.match(line):
            old_left = int(hunk.group("old") or 1)
            number = int(hunk.group("new"))
            new_left = int(hunk.group("count") or 1)


def _diff_path(raw: str) -> str | None:
    raw = raw.rstrip("\t").split("\t")[0]
    if raw.startswith('"') and raw.endswith('"') and len(raw) > 1:
        raw = codecs.escape_decode(raw[1:-1].encode("utf-8"))[0].decode("utf-8", "replace")
    if raw == "/dev/null":
        return None
    return raw.removeprefix("b/")
