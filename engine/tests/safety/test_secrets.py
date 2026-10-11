"""Tests for factory_engine.safety.secrets (task 2.10): the secret scan.

The secret fixtures are put together at run time from fragments, so this file holds no string
that a push-protection scanner would take for a real token.
"""

import random
import string
from pathlib import Path

import pytest

from factory_engine.safety.secrets import (
    SecretFinding,
    check_tool_content,
    scan_diff,
    scan_text,
)

CLEAN = Path(__file__).parent / "fixtures" / "clean"
UPPER = string.ascii_uppercase + string.digits
ALNUM = string.ascii_letters + string.digits
URLSAFE = ALNUM + "-_"
BASE64 = ALNUM + "+/"


def _body(n: int, alphabet: str, seed: int) -> str:
    rng = random.Random(seed)
    return "".join(rng.choice(alphabet) for _ in range(n))


def _pem(label: str, seed: int) -> tuple[str, str]:
    lines = [_body(64, BASE64, seed + i) for i in range(4)]
    block = "\n".join([f"-----BEGIN {label}-----", *lines, f"-----END {label}-----"])
    return block, lines[1]


ANT = "sk-" + "ant-"
ANT_API = ANT + "api03-" + _body(93, URLSAFE, 1) + "AA"
ANT_OAUTH = ANT + "oat01-" + _body(93, URLSAFE, 2) + "AA"
ANT_ADMIN = ANT + "admin01-" + _body(93, URLSAFE, 3) + "AA"
AWS_ID = "AK" + "IA" + _body(16, UPPER, 4)
AWS_TEMP_ID = "AS" + "IA" + _body(16, UPPER, 5)
AWS_SECRET = _body(40, BASE64, 6)
GH_CLASSIC = "gh" + "p_" + _body(36, ALNUM, 7)
GH_OAUTH = "gh" + "o_" + _body(36, ALNUM, 8)
GH_APP = "gh" + "s_" + _body(36, ALNUM, 9)
GH_USER = "gh" + "u_" + _body(36, ALNUM, 10)
GH_REFRESH = "gh" + "r_" + _body(36, ALNUM, 11)
GH_FINE = "github" + "_pat_" + _body(22, ALNUM, 12) + "_" + _body(59, ALNUM, 13)
RSA, RSA_LINE = _pem("RSA PRIVATE KEY", 20)
OPENSSH, OPENSSH_LINE = _pem("OPENSSH PRIVATE KEY", 30)
EC, EC_LINE = _pem("EC PRIVATE KEY", 40)
PKCS8, PKCS8_LINE = _pem("PRIVATE KEY", 50)
ENCRYPTED, ENCRYPTED_LINE = _pem("ENCRYPTED PRIVATE KEY", 60)
PGP, PGP_LINE = _pem("PGP PRIVATE KEY BLOCK", 70)
DB_PASSWORD = _body(24, ALNUM, 80)
STRIPE = "sk_" + "live_" + _body(24, ALNUM, 81)
SESSION = _body(32, ALNUM + "!@#%^*", 82)
OPENAI = "sk-" + "proj-" + _body(48, URLSAFE, 83)
SMTP = _body(16, ALNUM, 84)
URL_PASSWORD = _body(18, ALNUM, 85)
WORDS_PASSWORD = "correcthorsebatterystaple"

ANTHROPIC = "Anthropic key"
AWS_KEY_ID = "AWS access key ID"
AWS_SECRET_KEY = "AWS secret access key"
GITHUB = "GitHub token"
PRIVATE_KEY = "private key"
ENV = ".env secret"
URL = "password in URL"

# Each case: id, text to scan, the kind of finding, the secret value that must not leak.
SECRETS = [
    ("anthropic-api", ANT_API, ANTHROPIC, ANT_API),
    ("anthropic-oauth", f"CLAUDE_CODE_OAUTH_TOKEN={ANT_OAUTH}", ANTHROPIC, ANT_OAUTH),
    ("anthropic-admin", f'client = Anthropic(api_key="{ANT_ADMIN}")', ANTHROPIC, ANT_ADMIN),
    ("anthropic-json", f'{{"apiKey": "{ANT_API}"}}', ANTHROPIC, ANT_API),
    ("aws-key-id", f"aws_access_key_id = {AWS_ID}", AWS_KEY_ID, AWS_ID),
    ("aws-temp-key-id", f'"AccessKeyId": "{AWS_TEMP_ID}",', AWS_KEY_ID, AWS_TEMP_ID),
    ("aws-secret-ini", f"aws_secret_access_key = {AWS_SECRET}", AWS_SECRET_KEY, AWS_SECRET),
    ("aws-secret-env", f"AWS_SECRET_ACCESS_KEY={AWS_SECRET}", AWS_SECRET_KEY, AWS_SECRET),
    ("aws-secret-json", f'"SecretAccessKey": "{AWS_SECRET}"', AWS_SECRET_KEY, AWS_SECRET),
    ("github-classic", f"git clone https://{GH_CLASSIC}@github.com/a/b", GITHUB, GH_CLASSIC),
    ("github-oauth", f"token: {GH_OAUTH}", GITHUB, GH_OAUTH),
    ("github-app", f"GH_TOKEN={GH_APP}", GITHUB, GH_APP),
    ("github-user", f"auth = '{GH_USER}'", GITHUB, GH_USER),
    ("github-refresh", GH_REFRESH, GITHUB, GH_REFRESH),
    ("github-fine-grained", f"headers = {{'Authorization': 'Bearer {GH_FINE}'}}", GITHUB, GH_FINE),
    ("rsa-key", RSA, PRIVATE_KEY, RSA_LINE),
    ("openssh-key", OPENSSH, PRIVATE_KEY, OPENSSH_LINE),
    ("ec-key", EC, PRIVATE_KEY, EC_LINE),
    ("pkcs8-key", PKCS8, PRIVATE_KEY, PKCS8_LINE),
    ("encrypted-key", ENCRYPTED, PRIVATE_KEY, ENCRYPTED_LINE),
    ("pgp-key", PGP, PRIVATE_KEY, PGP_LINE),
    ("pem-in-json", '"key": "' + PKCS8.replace("\n", "\\n") + '"', PRIVATE_KEY, PKCS8_LINE),
    ("env-password", f"DB_PASSWORD={DB_PASSWORD}", ENV, DB_PASSWORD),
    ("env-export", f"export STRIPE_SECRET_KEY={STRIPE}", ENV, STRIPE),
    ("env-double-quoted", f'SESSION_SECRET="{SESSION}"', ENV, SESSION),
    ("env-single-quoted", f"OPENAI_API_KEY='{OPENAI}'", ENV, OPENAI),
    ("env-pass", f"  SMTP_PASS={SMTP}", ENV, SMTP),
    ("env-crlf", f"APP_NAME=demo\r\nAPI_TOKEN={DB_PASSWORD}\r\n", ENV, DB_PASSWORD),
    ("env-words", f"ADMIN_PASSWORD={WORDS_PASSWORD}", ENV, WORDS_PASSWORD),
    ("env-spaces", f"CLIENT_SECRET = {SESSION}", ENV, SESSION),
    ("url-password", f"DATABASE_URL=postgres://app:{URL_PASSWORD}@db:5432/app", URL, URL_PASSWORD),
    ("url-in-code", f'redis.from_url("redis://:{URL_PASSWORD}@cache:6379/0")', URL, URL_PASSWORD),
]
CLEAN_FILES = sorted(CLEAN.iterdir())


@pytest.mark.parametrize(
    ("text", "kind"), [(t, k) for _, t, k, _ in SECRETS], ids=[s[0] for s in SECRETS]
)
def test_every_fixture_secret_is_detected(text: str, kind: str) -> None:
    kinds = {finding.kind for finding in scan_text(text)}
    assert kind in kinds


@pytest.mark.parametrize("path", CLEAN_FILES, ids=[p.name for p in CLEAN_FILES])
def test_clean_fixture_has_no_hits(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert scan_text(text, path=path.name) == []


def test_clean_fixtures_cover_lock_files_base64_and_uuids() -> None:
    names = {p.name for p in CLEAN_FILES}
    assert {"uv.lock", "package-lock.json", "pnpm-lock.yaml"} <= names
    assert {"base64_data.txt", "uuids.txt"} <= names


def _windows(value: str, size: int = 8) -> set[str]:
    return {value[i : i + size] for i in range(len(value) - size + 1)}


@pytest.mark.parametrize(
    ("text", "value"), [(t, v) for _, t, _, v in SECRETS], ids=[s[0] for s in SECRETS]
)
def test_findings_never_include_the_secret_value(text: str, value: str) -> None:
    findings = scan_text(text, path="config/.env")
    assert findings
    for finding in findings:
        for shown in (str(finding), repr(finding), finding.describe(), finding.preview):
            assert value not in shown
            assert not any(part in shown for part in _windows(value)), shown


def test_blocked_reason_never_includes_the_secret_value() -> None:
    decision = check_tool_content("Write", {"file_path": "a.py", "content": f"KEY = '{GH_FINE}'"})
    assert not decision.allowed
    assert not any(part in decision.reason for part in _windows(GH_FINE))


def test_finding_reports_the_line_and_path() -> None:
    text = f"first\nsecond\nGH_TOKEN={GH_CLASSIC}\n"
    [finding] = scan_text(text, path="deploy/.env")
    assert (finding.kind, finding.line, finding.path) == (GITHUB, 3, "deploy/.env")
    assert "deploy/.env:3" in finding.describe()


def test_one_finding_per_secret_on_a_line() -> None:
    findings = scan_text(f"AWS_ACCESS_KEY_ID={AWS_ID}  # and {AWS_TEMP_ID}")
    assert [f.kind for f in findings].count(AWS_KEY_ID) == 2
    assert len(findings) == 2


def test_private_key_block_is_one_finding() -> None:
    findings = scan_text(f"intro\n{RSA}\noutro\n")
    assert [(f.kind, f.line) for f in findings] == [(PRIVATE_KEY, 2)]


# ------------------------------------------------------------------ diffs


def _diff(*hunks: str, path: str = "src/app.py") -> str:
    head = (
        f"diff --git a/{path} b/{path}\nindex 1111111..2222222 100644\n--- a/{path}\n+++ b/{path}\n"
    )
    return head + "".join(hunks)


def test_diff_reports_added_lines_with_path_and_new_line_number() -> None:
    diff = _diff(
        "@@ -10,3 +10,4 @@ def main():\n context\n-old = 1\n+new = 1\n"
        f"+GH = '{GH_CLASSIC}'\n context\n"
    )
    [finding] = scan_diff(diff)
    assert (finding.kind, finding.path, finding.line) == (GITHUB, "src/app.py", 12)


def test_diff_ignores_removed_and_context_lines() -> None:
    diff = _diff(
        f"@@ -1,2 +1,2 @@\n token = '{GH_CLASSIC}'\n-AWS = '{AWS_ID}'\n+AWS = os.environ['AWS']\n"
    )
    assert scan_diff(diff) == []


def test_diff_added_line_that_looks_like_a_header_is_scanned() -> None:
    diff = _diff(f"@@ -0,0 +1,2 @@\n+++ {GH_CLASSIC}\n+--- {AWS_ID}\n", path="notes.txt")
    kinds = sorted(f.kind for f in scan_diff(diff))
    assert kinds == [AWS_KEY_ID, GITHUB]


def test_diff_new_file_and_several_files() -> None:
    new = (
        "diff --git a/.env.local b/.env.local\nnew file mode 100644\nindex 0000000..3333333\n"
        f"--- /dev/null\n+++ b/.env.local\n@@ -0,0 +1,2 @@\n+APP=demo\n+DB_PASSWORD={DB_PASSWORD}\n"
    )
    clean = _diff("@@ -5 +5 @@\n-x = 1\n+x = 2\n", path="src/clean.py")
    lock = _diff("@@ -1 +1 @@\n-a\n+b\n", path="uv.lock")
    findings = scan_diff(clean + new + lock)
    assert [(f.kind, f.path, f.line) for f in findings] == [(ENV, ".env.local", 2)]


def test_diff_private_key_added_in_a_new_file() -> None:
    added = "".join(f"+{line}\n" for line in OPENSSH.splitlines())
    count = len(OPENSSH.splitlines())
    diff = _diff(f"@@ -0,0 +1,{count} @@\n{added}", path="deploy/id_ed25519")
    assert [(f.kind, f.line) for f in scan_diff(diff)] == [(PRIVATE_KEY, 1)]


def test_diff_quoted_path_and_no_newline_marker() -> None:
    diff = (
        'diff --git "a/my dir/x.env" "b/my dir/x.env"\n--- "a/my dir/x.env"\n+++ "b/my dir/x.env"\n'
        f"@@ -1 +1 @@\n-API_TOKEN=\n\\ No newline at end of file\n+API_TOKEN={DB_PASSWORD}\n"
        "\\ No newline at end of file\n"
    )
    [finding] = scan_diff(diff)
    assert (finding.path, finding.line) == ("my dir/x.env", 1)


def test_clean_diff_has_no_hits() -> None:
    added = "".join(
        f"+{line}\n" for line in (CLEAN / "uv.lock").read_text(encoding="utf-8").splitlines()
    )
    count = added.count("\n")
    assert scan_diff(_diff(f"@@ -0,0 +1,{count} @@\n{added}", path="uv.lock")) == []


# ------------------------------------------------------------------ tool content


TOOL_CASES = [
    ("write", "Write", {"file_path": "src/a.py", "content": f"KEY = '{ANT_API}'\n"}, False),
    (
        "write-clean",
        "Write",
        {"file_path": "src/a.py", "content": "KEY = os.environ['KEY']\n"},
        True,
    ),
    (
        "edit",
        "Edit",
        {
            "file_path": "src/a.py",
            "old_string": "KEY = None",
            "new_string": f"KEY = '{GH_CLASSIC}'",
        },
        False,
    ),
    (
        "edit-clean",
        "Edit",
        {"file_path": "src/a.py", "old_string": f"K='{GH_CLASSIC}'", "new_string": "K=None"},
        True,
    ),
    (
        "multi-edit",
        "MultiEdit",
        {
            "file_path": "src/a.py",
            "edits": [
                {"old_string": "a", "new_string": "b"},
                {"old_string": "c", "new_string": f"AWS_SECRET_ACCESS_KEY={AWS_SECRET}"},
            ],
        },
        False,
    ),
    (
        "multi-edit-clean",
        "MultiEdit",
        {"file_path": "src/a.py", "edits": [{"old_string": "a", "new_string": "b"}]},
        True,
    ),
    ("notebook", "NotebookEdit", {"notebook_path": "n.ipynb", "new_source": RSA}, False),
    ("notebook-delete", "NotebookEdit", {"notebook_path": "n.ipynb", "edit_mode": "delete"}, True),
    ("read", "Read", {"file_path": "src/a.py"}, True),
    ("write-not-text", "Write", {"file_path": "src/a.py", "content": 5}, False),
    ("write-missing", "Write", {"file_path": "src/a.py"}, False),
    (
        "edit-not-text",
        "Edit",
        {"file_path": "src/a.py", "old_string": "a", "new_string": ["b"]},
        False,
    ),
    ("multi-edit-not-list", "MultiEdit", {"file_path": "src/a.py", "edits": "b"}, False),
    (
        "multi-edit-bad-item",
        "MultiEdit",
        {"file_path": "src/a.py", "edits": [{"new_string": 1}]},
        False,
    ),
]


@pytest.mark.parametrize(
    ("tool", "tool_input", "allowed"), [c[1:] for c in TOOL_CASES], ids=[c[0] for c in TOOL_CASES]
)
def test_check_tool_content(tool: str, tool_input: dict[str, object], allowed: bool) -> None:
    decision = check_tool_content(tool, tool_input)
    assert decision.allowed is allowed, decision.reason
    if not allowed:
        assert decision.reason.startswith("Blocked: ")


def test_blocked_write_names_the_kind_file_and_line() -> None:
    content = f"import os\nTOKEN = '{GH_CLASSIC}'\n"
    decision = check_tool_content("Write", {"file_path": "src/conf.py", "content": content})
    assert not decision.allowed
    assert GITHUB in decision.reason
    assert "src/conf.py:2" in decision.reason
    assert ".env.factory" in decision.reason  # tells the agent where test secrets belong


def test_finding_is_a_frozen_value() -> None:
    finding = SecretFinding(kind=GITHUB, line=1, preview="ghp_…")
    with pytest.raises(AttributeError):
        finding.line = 2  # type: ignore[misc]  # frozen dataclass: assignment must fail
