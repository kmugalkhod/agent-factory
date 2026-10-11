"""Pull requests and merges through the GitHub CLI (design: "Git flow", steps 5 and 6).

`gh` runs with the user's own login (setup step S.2), with a list of arguments, no shell and no
prompts. The body is published, so it goes through the secret redaction first, and it is passed
as a file in the run folder (kept as a record), never on the command line.
"""

import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from factory_engine.errors import GitHubError
from factory_engine.git_ops import find_program, run_branch, run_command
from factory_engine.safety.secrets import redact

GH_TIMEOUT = 120.0
MAX_BODY = 60_000
"""GitHub refuses a body over 65,536 characters; keep a margin."""
BODY_FILE = "pr-body.md"
MAX_PLAN = 20_000
MAX_FINDINGS = 15_000
MAX_LINE = 2_000
"""A longer line is left out of the body: redacting it is slow, and cutting it could split a
secret from the name that marks it."""
MAX_FENCE = 16
"""The longest backtick fence around the test output; longer runs that start a line are
indented so they can't close it."""
_FENCE_LIKE = re.compile(rf"^(?= {{0,3}}`{{{MAX_FENCE},}})", re.MULTILINE)
_PR_URL = re.compile(r"^https://\S+/pull/(?P<number>\d+)$")
_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


@dataclass(frozen=True)
class GitHub:
    """Runs `gh` for the engine. `env` is the engine's environment (pass `os.environ`)."""

    env: Mapping[str, str]
    timeout: float = GH_TIMEOUT

    def run(self, args: Sequence[str], cwd: Path) -> str:
        """Run `gh <args>` in `cwd` and return its standard output."""
        shown = "gh " + " ".join(args)
        program = find_program("gh", self.env)
        if program is None:
            raise GitHubError(
                f"`{shown}`: gh isn't on PATH. Install the GitHub CLI and run `gh auth login`."
            )
        # GH_REPO would point gh at another repository than the one `cwd` is in.
        env = {k: v for k, v in self.env.items() if k.upper() != "GH_REPO"}
        env.update(GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1")
        try:
            done = run_command([str(program), *args], cwd, env, self.timeout)
        except subprocess.TimeoutExpired as err:
            raise GitHubError(
                f"`{shown}` took longer than {self.timeout:g}s and was stopped. Check the "
                "network and `gh auth status`, then retry."
            ) from err
        except (OSError, ValueError) as err:
            raise GitHubError(f"`{shown}` couldn't start ({err}).") from err
        if done.returncode != 0:
            detail = (done.stderr.strip() or done.stdout.strip())[-2000:]
            raise GitHubError(f"`{shown}` failed (exit {done.returncode}): {detail}")
        return done.stdout


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str


@dataclass(frozen=True)
class MergeApproval:
    """Your approve action for one pull request. No merge happens without one."""

    pr_number: int


def pr_body(*, run_label: str, plan: str, test_output: str, findings: str) -> str:
    """The pull request body: the plan, the test output and the review findings. Long parts are
    cut (the test output keeps its end, where failures are) so the body fits GitHub's limit.
    Each part is redacted before the final cut, since a cut can drop the name that marks a
    value as a secret."""
    head = f"Opened by agent-factory for {run_label}.\n\n## Plan\n\n"
    plan_part = _redacted(plan, MAX_PLAN, keep_end=False)
    findings_part = "\n\n## Review findings\n\n" + _redacted(findings, MAX_FINDINGS, keep_end=False)
    fixed = len(head) + len(plan_part) + len(findings_part) + len("\n\n## Test output\n\n")
    # Fence lines, three newlines, and four spaces to indent a line the cut starts mid-run.
    room = MAX_BODY - fixed - 2 * MAX_FENCE - 3 - 4
    output = _unfence(_redacted(_unfence(test_output), room, keep_end=True))
    longest = max((len(run) for run in re.findall(r"`+", output)), default=0)
    fence = "`" * min(MAX_FENCE, max(3, longest + 1))
    return f"{head}{plan_part}\n\n## Test output\n\n{fence}\n{output}\n{fence}{findings_part}\n"


def open_pr(
    gh: GitHub,
    repo: Path,
    run_folder: Path,
    *,
    run_id: int,
    slug: str,
    title: str,
    body: str,
    base: str = "main",
) -> PullRequest:
    """Open a pull request from the run branch into `base`. The body is redacted and saved as
    `pr-body.md` in the run folder, then passed to `gh` as a file."""
    head = run_branch(run_id, slug)
    text = _keep_start(redact(body), MAX_BODY)
    body_file = run_folder / BODY_FILE
    try:
        with body_file.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except OSError as err:
        raise GitHubError(f"{body_file}: can't write the pull request body ({err}).") from err
    out = gh.run(
        [
            "pr",
            "create",
            "--base",
            base,
            "--head",
            head,
            "--title",
            redact(title),
            "--body-file",
            str(body_file),
        ],
        repo,
    )
    lines = out.strip().splitlines()
    found = _PR_URL.match(lines[-1].strip()) if lines else None
    if found is None:
        raise GitHubError(
            f"`gh pr create` for {head} didn't print a pull request URL: {out.strip()[-500:]!r}. "
            "Check the repo on GitHub before retrying, so no second pull request is opened."
        )
    return PullRequest(number=int(found.group("number")), url=found.group(0))


def merge_pr(
    gh: GitHub, repo: Path, pr: PullRequest, *, head_sha: str, approval: MergeApproval
) -> None:
    """Merge the pull request on your approval, only if its head is still `head_sha` (the
    commit whose tests passed), so nothing untested is merged."""
    if approval.pr_number != pr.number:
        raise GitHubError(
            f"pull request #{pr.number} has no approval (the approval given is for "
            f"#{approval.pr_number}). Approve this pull request to merge it."
        )
    found = _PR_URL.fullmatch(pr.url)
    if found is None or int(found.group("number")) != pr.number:
        raise GitHubError(
            f"{pr.url!r} isn't the URL of pull request #{pr.number}. Pass the pull request "
            "`open_pr` returned."
        )
    if not _COMMIT.fullmatch(head_sha):
        raise GitHubError(
            f"{head_sha!r} isn't a full commit ID. Pass the tested head commit of #{pr.number}."
        )
    gh.run(["pr", "merge", pr.url, "--merge", "--match-head-commit", head_sha], repo)


def _redacted(text: str, limit: int, *, keep_end: bool) -> str:
    """`text` redacted and cut to `limit`, keeping its start or its end. Whole lines are cut
    before redacting (`redact` works line by line, so this never splits a name from its value)
    and lines over `MAX_LINE` are left out."""
    lines = [
        line
        if len(line) <= MAX_LINE
        else f"… (a line of {len(line)} characters left out; the full text is in the run folder)"
        for line in text.strip().split("\n")
    ]
    budget = limit - max(len(_CUT_START), len(_CUT_END))
    kept: list[str] = []
    size = 0
    for line in reversed(lines) if keep_end else lines:
        size += len(line) + 1
        if size > budget:
            break
        kept.append(line)
    if keep_end:
        kept.reverse()
    out = redact("\n".join(kept))
    if len(kept) < len(lines):
        out = _CUT_START + out if keep_end else out + _CUT_END
    # Masks never lengthen a line; cut anyway so the limit holds whatever `redact` does.
    return _keep_end(out, limit) if keep_end else _keep_start(out, limit)


def _unfence(text: str) -> str:
    """Indent each line that starts with `MAX_FENCE` or more backticks by four spaces, which
    Markdown never reads as a closing fence, so no line of the text can close the fence."""
    return _FENCE_LIKE.sub("    ", text)


_CUT_END = "\n\n… (cut; the full text is in the run folder)"
_CUT_START = "… (earlier output cut; the full output is in the run folder)\n"


def _keep_start(text: str, limit: int) -> str:
    note = _CUT_END
    return text if len(text) <= limit else text[: max(0, limit - len(note))] + note


def _keep_end(text: str, limit: int) -> str:
    note = _CUT_START
    return text if len(text) <= limit else note + text[len(text) - max(0, limit - len(note)) :]
