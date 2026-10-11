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
        env = {**self.env, "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"}
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
    cut (the test output keeps its end, where failures are) so the body fits GitHub's limit."""
    head = f"Opened by agent-factory for {run_label}.\n\n## Plan\n\n"
    plan_part = _keep_start(plan.strip(), MAX_PLAN)
    findings_part = "\n\n## Review findings\n\n" + _keep_start(findings.strip(), MAX_FINDINGS)
    fixed = len(head) + len(plan_part) + len(findings_part) + len("\n\n## Test output\n\n")
    fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", test_output)), default=0))
    room = MAX_BODY - fixed - 2 * len(fence) - 3  # two newlines in the fence, one at the end
    tests_part = f"{fence}\n{_keep_end(test_output.strip(), room)}\n{fence}"
    return f"{head}{plan_part}\n\n## Test output\n\n{tests_part}{findings_part}\n"


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
    if not _COMMIT.fullmatch(head_sha):
        raise GitHubError(
            f"{head_sha!r} isn't a full commit ID. Pass the tested head commit of #{pr.number}."
        )
    gh.run(["pr", "merge", str(pr.number), "--merge", "--match-head-commit", head_sha], repo)


def _keep_start(text: str, limit: int) -> str:
    note = "\n\n… (cut; the full text is in the run folder)"
    return text if len(text) <= limit else text[: max(0, limit - len(note))] + note


def _keep_end(text: str, limit: int) -> str:
    note = "… (earlier output cut; the full output is in the run folder)\n"
    return text if len(text) <= limit else note + text[len(text) - max(0, limit - len(note)) :]
