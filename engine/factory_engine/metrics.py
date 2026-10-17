"""Run metrics (design: "Quality metrics"; task 2.27).

Pure updates of a run's `Metrics`, called where the engine sees each fact:

- `token_use`: each agent turn's tokens, dated and per attempt (`SessionRecorder`);
- `pause_hit`: each pause, usage limits included, kept after the run carries on (watchdog);
- `builder_attempt`: builder retries, the builder attempts after the first (pipeline);
- `builder_gate` and `review_verdict`: first-pass success, decided once: False as soon as a
  builder gate fails or a review finds an Important problem, True when the first review that
  decides anything approves with no failed build before it. A malformed review decides nothing,
  nor does a rebase conflict sent to the builder;
- Important findings are counted by the review gate (`gates.review.record_findings`).

`lines_changed` measures the lines you changed on the pull request before the merge: lines
added or removed in the run's own change between the head the pull request was opened at (what
you were first shown) and the head merged. Main's changes, brought in by rebases, aren't
counted: the opened change is carried onto the merged head's base (`git merge-tree`) and
compared there. The engine merges only the head it tested, so what changes a pull request after
it opens is what you asked for (feedback reruns, 2.23); commits pushed to the run branch by
anyone else make the engine's push or merge refuse, and the run needs you.
"""

from datetime import datetime
from pathlib import Path

from factory_engine.git_ops import Git
from factory_engine.run import Metrics, Pause, PauseRecord, Role, TokenUse

BASE_REF = "refs/remotes/origin/main"


# ------------------------------------------------------------------ tokens and pauses


def token_use(
    metrics: Metrics,
    *,
    role: Role,
    attempt: int,
    at: datetime,
    input: int,
    output: int,
    cache_read: int = 0,
    cache_creation: int = 0,
) -> Metrics:
    """`metrics` with one turn's tokens added: a dated record and the role's total."""
    use = TokenUse(
        role=role,
        attempt=attempt,
        at=at,
        input=input,
        output=output,
        cache_read=cache_read,
        cache_creation=cache_creation,
    )
    tokens = dict(metrics.tokens)
    tokens[role] = tokens.get(role, 0) + use.total
    return metrics.model_copy(update={"tokens": tokens, "token_use": [*metrics.token_use, use]})


def pause_hit(metrics: Metrics, pause: Pause, *, at: datetime) -> Metrics:
    """`metrics` with the pause the run just went into."""
    record = PauseRecord(
        reason=pause.reason,
        role=pause.role,
        attempt=pause.attempt,
        at=at,
        resume_at=pause.resume_at,
    )
    return metrics.model_copy(update={"pauses": [*metrics.pauses, record]})


def tokens_per_attempt(metrics: Metrics) -> dict[tuple[Role, int], int]:
    """Tokens per (role, attempt), in the order the attempts first used any."""
    totals: dict[tuple[Role, int], int] = {}
    for use in metrics.token_use:
        key = (use.role, use.attempt)
        totals[key] = totals.get(key, 0) + use.total
    return totals


def total_tokens(metrics: Metrics) -> int:
    return sum(metrics.tokens.values())


def usage_limit_hits(metrics: Metrics) -> int:
    return sum(1 for pause in metrics.pauses if pause.reason == "usage_limit")


# ------------------------------------------------------------------ retries and first pass


def builder_attempt(metrics: Metrics, attempt: int) -> Metrics:
    """`metrics` once builder attempt `attempt` starts. Never goes down."""
    retries = max(metrics.builder_retries, attempt - 1)
    return metrics.model_copy(update={"builder_retries": retries})


def builder_gate(metrics: Metrics, passed: bool) -> Metrics:
    """A builder gate result: a failure before first pass is decided makes it False."""
    if metrics.first_pass is None and not passed:
        return metrics.model_copy(update={"first_pass": False})
    return metrics


def review_verdict(metrics: Metrics, approved: bool) -> Metrics:
    """A review that decided (approved, or found Important problems): the first one decides
    first pass, unless a failed build decided it already."""
    if metrics.first_pass is None:
        return metrics.model_copy(update={"first_pass": approved})
    return metrics


# ------------------------------------------------------------------ lines you changed


def lines_changed(
    git: Git, cwd: Path, *, opened: str, merged: str, base_ref: str = BASE_REF
) -> int | None:
    """Lines added plus lines removed in the run's change between `opened` and `merged`, with
    main's changes (from `base_ref`, fetched) left out. None when the opened change can't be
    carried cleanly onto the merged head's base, so there is nothing fair to compare with.
    Raises GitError when git can't answer."""
    if opened == merged:
        return 0
    old_base = git.out(["merge-base", base_ref, opened], cwd)
    new_base = git.out(["merge-base", base_ref, merged], cwd)
    compare = opened
    if old_base != new_base:
        done = git.run(
            [
                "merge-tree",
                "--write-tree",
                "--no-messages",
                f"--merge-base={old_base}",
                new_base,
                opened,
            ],
            cwd,
            check=False,
        )
        if done.returncode != 0:
            return None  # 1: conflicts; anything else: git couldn't merge the trees
        compare = done.stdout.split()[0]
    numstat = git.out(["diff", "--numstat", "--no-ext-diff", "--no-textconv", compare, merged], cwd)
    return sum(_numstat_lines(line) for line in numstat.splitlines())


def _numstat_lines(line: str) -> int:
    """`<added>\\t<removed>\\t<path>`; a binary file shows `-` and counts no lines."""
    added, removed, *_ = [*line.split("\t"), "", ""]
    return sum(int(part) for part in (added, removed) if part.isdigit())


def describe_tokens(metrics: Metrics) -> str:
    """`<total> (<role> <n>, ...)`, roles in pipeline order."""
    order: tuple[Role, ...] = ("planner", "tester", "builder", "reviewer")
    parts = [f"{role} {metrics.tokens[role]}" for role in order if role in metrics.tokens]
    shown = f" ({', '.join(parts)})" if parts else ""
    return f"{total_tokens(metrics)}{shown}"
