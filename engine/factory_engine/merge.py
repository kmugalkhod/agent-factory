"""Update before review, push, pull request and merge (design: "Git flow", steps 3 to 7).

`GitFlow` is the pipeline's `Hooks` for a repo with a remote on GitHub:

- `update`, in `updating` before the review: check the worktree is exactly the run branch's
  head and the test lock holds, then fetch main and rebase the run branch onto it.
  - No conflicts: the test lock follows main's changes to the tester's files (their blob ids
    are taken from the rebased head), and the builder gate reruns the tests on the result. A
    pass that left the worktree exactly the tested head goes on to the review; anything else
    needs you.
  - Conflicts: the builder resumes, told the conflicted files (→ building), if it has an
    attempt left and none of them is locked; otherwise the rebase is given up and the run needs
    you. How the rebase carries on after the builder's edits is in `pipeline.py`.
- `publish`, once the review passed: push exactly the worktree's head (the safe force-push)
  and open the pull request, or move an open one to the new head. Its number, URL and head go
  to `run.json`.
- `merge`, in `awaiting_merge` once your approval is recorded (`decisions.approve_merge`): if
  main moved since the pull request's head was tested, rebase onto it, rerun the tests and push
  the tested commit, then fetch main again (at most `MERGE_REBASES` rebases). Then merge through
  `gh`, pinned to the tested head, and remove the worktree (→ merged). Anything that fails stops
  the run for you, with nothing merged. `gh pr merge` can pin the head but not main: a push to
  main between the last fetch and the merge isn't seen, which branch protection's "Require
  branches to be up to date before merging" closes on GitHub's side.

No code path merges without your approval: `merge_approved` is the engine's only call of
`github.merge_pr`, and it refuses unless `run.json` holds an approval for this pull request at
its head (or at the head the engine rebased at merge time) and the run's last decision event
is that `approve_merge`.
"""

from collections.abc import Sequence
from pathlib import Path

from factory_engine.errors import (
    ConfigError,
    FactoryError,
    GateError,
    GitError,
    GitHubError,
    MergeError,
)
from factory_engine.events import Decision, GateResult, read_events
from factory_engine.gates.build import check_lock
from factory_engine.gates.plan import PLAN_FILE
from factory_engine.gates.tests_gate import load_tester_files, rebase_tester_files
from factory_engine.git_ops import (
    main_moved,
    push_run_branch,
    rebase_abort,
    rebase_in_progress,
    rebase_onto_main,
    remove_worktree,
    run_branch,
    worktree_changes,
)
from factory_engine.github import GitHub, MergeApproval, PullRequest, merge_pr, open_pr, pr_body
from factory_engine.pipeline import (
    REVIEW_FILE,
    UPDATE_GATE,
    Hooks,
    Pipeline,
    conflict_message,
    lock_rebased,
    locked_conflict_reason,
    locked_conflicts,
)
from factory_engine.run import PullRequestInfo, Run

MERGE_UPDATE_GATE = "merge.update"
"""The `gate_result` of a rebase onto a main that moved after your approval, and its tests."""
CLEANUP_GATE = "merge.cleanup"
"""The `gate_result` of removing the worktree after the merge."""
MERGE_REBASES = 3
"""How many times main may move during one merge (each a rebase and a test run) before the run
needs you."""
_TITLE_UNSAFE = '"%^&|<>!\r\n'  # what a `gh.cmd` launcher can't be given (git_ops.run_command)


def approval_text(pr_number: int, head: str) -> str:
    """The text of the `approve_merge` decision event: which pull request, at which head."""
    return f"merge pull request #{pr_number} at {head}"


def check_approval(run: Run, run_folder: Path) -> MergeApproval:
    """Your approval to merge the run's pull request as it is now. Raises MergeError unless the
    run waits for its merge, `run.json` records an approval for this pull request at its head
    (or at the head the engine rebased onto a newer main), and the last decision in the event
    log is that approval."""
    pr, approval = run.pull_request, run.merge_approval
    label = f"run {run.repo}/{run.id}"
    if run.state != "awaiting_merge":
        raise MergeError(f"{label} is {run.state}, not waiting for its merge, so nothing merges.")
    if pr is None:
        raise MergeError(
            f"{label} has no pull request recorded, so nothing merges. The review's publish "
            "step opens it."
        )
    if approval is None:
        raise MergeError(
            f"pull request #{pr.number} of {label} has no approval recorded, so it isn't "
            "merged. Approve the merge first."
        )
    if approval.pr_number != pr.number or approval.head not in (pr.head, pr.rebased_from):
        raise MergeError(
            f"the approval recorded is for pull request #{approval.pr_number} at "
            f"{approval.head}, but {label}'s pull request is #{pr.number} at {pr.head}. Look at "
            "the pull request again and approve the merge again."
        )
    decisions = [
        event.body for event in read_events(run_folder) if isinstance(event.body, Decision)
    ]
    last = decisions[-1] if decisions else None
    expected = approval_text(approval.pr_number, approval.head)
    if last is None or last.action != "approve_merge" or last.text != expected:
        raise MergeError(
            f"{label}'s event log has no approve_merge decision matching the approval in "
            "run.json, so it isn't trusted. Approve the merge again."
        )
    return MergeApproval(pr_number=pr.number)


def merge_approved(gh: GitHub, repo: Path, run: Run, run_folder: Path) -> None:
    """Merge the run's pull request on your approval (`check_approval`), only while GitHub's
    head is the tested head. The engine's only merge."""
    approval = check_approval(run, run_folder)
    pr = run.pull_request
    if pr is None:  # check_approval refuses this already
        raise MergeError(f"run {run.repo}/{run.id} has no pull request, so nothing merges.")
    merge_pr(
        gh, repo, PullRequest(number=pr.number, url=pr.url), head_sha=pr.head, approval=approval
    )


def pr_title(run: Run) -> str:
    """`factory(run-<id>): <slug in words>`."""
    words = run.slug.replace("-", " ")
    return "".join(ch for ch in f"factory(run-{run.id}): {words}" if ch not in _TITLE_UNSAFE)


def pr_text(run: Run, run_folder: Path) -> str:
    """The pull request body: `plan.md`, the latest test output and `review.md`."""
    builder = max(1, run.attempts.get("builder", 0))
    return pr_body(
        run_label=f"run {run.id} ({run.slug})",
        plan=_read(run_folder / PLAN_FILE),
        test_output=_read(run_folder / f"test-output-builder-{builder}.txt"),
        findings=_read(run_folder / REVIEW_FILE),
    )


def _read(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8", errors="replace")
    except OSError as err:
        return f"({path.name} couldn't be read: {err})"


class GitFlow(Hooks):
    """The git flow's hooks: `gh` runs with your login, `remote` and `base` name main."""

    def __init__(self, gh: GitHub, *, remote: str = "origin", base: str = "main") -> None:
        self.gh = gh
        self.remote = remote
        self.base = base

    # ------------------------------------------------------------------ update before review

    async def update(self, pipeline: Pipeline, run: Run) -> Run:
        git, worktree = pipeline.git, pipeline.places.worktree
        run = pipeline.run
        try:
            if rebase_in_progress(git, worktree, run_id=run.id, slug=run.slug):
                rebase_abort(git, worktree, run_id=run.id, slug=run.slug)  # start it again
            problem = _before_rebase(pipeline)
            if problem is not None:
                return pipeline.stop_for_you(None, f"the run isn't rebased onto main: {problem}")
            step = rebase_onto_main(
                git, worktree, run_id=run.id, slug=run.slug, remote=self.remote, base=self.base
            )
        except (GitError, GateError) as err:
            return pipeline.stop_for_you(None, f"the rebase onto {self.base} couldn't run: {err}")
        if step.conflicts:
            return self._conflicts(pipeline, run, step.conflicts)
        tested = _retest(pipeline, UPDATE_GATE, f"after the rebase onto {self.base}")
        return tested if isinstance(tested, Run) else pipeline.run

    def _conflicts(self, pipeline: Pipeline, run: Run, files: Sequence[str]) -> Run:
        listed = ", ".join(files)
        problem: str | None = None
        try:
            locked = locked_conflicts(pipeline.places.run_folder, files)
            if locked:
                problem = locked_conflict_reason(locked).removesuffix(".")
        except GateError as err:
            problem = f"the test lock can't be read ({err})"
        if problem is None and pipeline.attempts_left("builder") == 0:
            problem = (
                f"the rebase onto {self.base} stopped on conflicts in {listed}, and the builder "
                "has used all its attempts, so nobody can resolve them"
            )
        if problem is not None:
            try:
                rebase_abort(pipeline.git, pipeline.places.worktree, run_id=run.id, slug=run.slug)
                undone = "The rebase was given up, so the run branch is as it was."
            except GitError as err:
                undone = f"The rebase couldn't be given up ({err}); run `git rebase --abort`."
            return pipeline.stop_for_you(None, f"{problem}. {undone}")
        pipeline.events.append(
            GateResult(
                gate=UPDATE_GATE,
                passed=False,
                reason=f"the rebase onto {self.base} stopped on conflicts in {listed}; the "
                "builder resolves them.",
            ),
            role="builder",
        )
        pipeline.tell_builder(conflict_message(pipeline.places, files))
        return pipeline.move_to("building")

    # ------------------------------------------------------------------ push and pull request

    async def publish(self, pipeline: Pipeline, run: Run) -> Run:
        places, git = pipeline.places, pipeline.git
        run = pipeline.run
        try:
            head = git.out(["rev-parse", "HEAD"], places.worktree)
            changes = worktree_changes(git, places.worktree)
            if changes:
                raise GitError(
                    f"the worktree has changes that aren't committed ({_listed(changes)}), so "
                    f"what was reviewed isn't exactly {head}"
                )
            push_run_branch(
                git,
                places.worktree,
                run_id=run.id,
                slug=run.slug,
                branch=run_branch(run.id, run.slug),
                remote=self.remote,
                commit=head,
            )
        except GitError as err:
            return pipeline.stop_for_you(None, f"the run branch couldn't be pushed: {err}")
        if run.pull_request is not None:
            info = run.pull_request.model_copy(update={"head": head, "rebased_from": None})
        else:
            try:
                pr = open_pr(
                    self.gh,
                    places.repo,
                    places.run_folder,
                    run_id=run.id,
                    slug=run.slug,
                    title=pr_title(run),
                    body=pr_text(run, places.run_folder),
                    base=self.base,
                )
            except GitHubError as err:
                return pipeline.stop_for_you(
                    None, f"the run branch is pushed, but the pull request wasn't opened: {err}"
                )
            info = PullRequestInfo(number=pr.number, url=pr.url, head=head)
        return pipeline.record(
            run.model_copy(update={"pull_request": info, "merge_approval": None})
        )

    # ------------------------------------------------------------------ merge

    async def merge(self, pipeline: Pipeline, run: Run) -> Run:
        """Merge on your approval. Main is fetched again after every rebase and push, so the
        merge starts only once main holds nothing the tested head lacks; if main keeps moving,
        `MERGE_REBASES` rebases are tried before the run needs you."""
        places, git = pipeline.places, pipeline.git
        run = pipeline.run
        try:
            check_approval(run, places.run_folder)
            pr = run.pull_request
            if pr is None:
                raise MergeError("no pull request is recorded")
            _require_tested(pipeline, pr.head)
        except FactoryError as err:
            return pipeline.stop_for_you(None, f"nothing was merged: {err}")
        approved = run.merge_approval.head if run.merge_approval is not None else pr.head
        tested = pr.head
        rebases = 0
        while True:
            try:
                moved = main_moved(git, places.worktree, remote=self.remote, base=self.base)
            except GitError as err:
                return pipeline.stop_for_you(None, f"nothing was merged: {err}")
            if not moved and tested == pr.head:
                break
            if not moved:  # push what was tested, then look at main again
                try:
                    push_run_branch(
                        git,
                        places.worktree,
                        run_id=run.id,
                        slug=run.slug,
                        branch=run_branch(run.id, run.slug),
                        remote=self.remote,
                        commit=tested,
                    )
                except GitError as err:
                    return pipeline.stop_for_you(
                        None,
                        f"the rebased run branch couldn't be pushed, so nothing was merged: {err}",
                    )
                pr = pr.model_copy(update={"head": tested, "rebased_from": approved})
                run = pipeline.record(run.model_copy(update={"pull_request": pr}))
                continue
            if rebases == MERGE_REBASES:
                return pipeline.stop_for_you(
                    None,
                    f"{self.base} moved again after each of {MERGE_REBASES} rebases and test "
                    "runs, so nothing was merged. Resume the run when it is quieter.",
                )
            rebases += 1
            outcome = self._rebase_for_merge(pipeline, run, pr)
            if isinstance(outcome, Run):
                return outcome
            tested = outcome
        try:
            _require_tested(pipeline, pr.head)
            merge_approved(self.gh, places.repo, run, places.run_folder)
        except FactoryError as err:
            return pipeline.stop_for_you(None, f"the merge failed: {err}")
        pipeline.record(run.model_copy(update={"merge_approval": None}))
        pipeline.move_to("merged")
        self._clean_up(pipeline)
        return pipeline.run

    def _rebase_for_merge(self, pipeline: Pipeline, run: Run, pr: PullRequestInfo) -> str | Run:
        """Main moved after your approval: rebase onto it and rerun the tests. Returns the new
        head, tested and not yet pushed, or the run, stopped for you."""
        git, worktree = pipeline.git, pipeline.places.worktree
        try:
            problem = _before_rebase(pipeline)
            if problem is not None:
                return pipeline.stop_for_you(
                    None, f"{self.base} moved after your approval, but {problem}"
                )
            step = rebase_onto_main(
                git, worktree, run_id=run.id, slug=run.slug, remote=self.remote, base=self.base
            )
            if step.conflicts:
                rebase_abort(git, worktree, run_id=run.id, slug=run.slug)
                locked = locked_conflicts(pipeline.places.run_folder, step.conflicts)
                also = f" ({locked_conflict_reason(locked)})" if locked else ""
                return pipeline.stop_for_you(
                    None,
                    f"{self.base} moved after your approval, and the rebase onto it stopped on "
                    f"conflicts in {', '.join(step.conflicts)}{also}. The rebase was given up "
                    f"and nothing was merged; pull request #{pr.number} is as you approved it.",
                )
        except (GateError, GitError) as err:
            return pipeline.stop_for_you(
                None,
                f"{self.base} moved after your approval, and the rebase onto it couldn't run, "
                f"so nothing was merged: {err}{_still_rebasing(pipeline, run)}",
            )
        return _retest(
            pipeline,
            MERGE_UPDATE_GATE,
            f"after the rebase onto the {self.base} that moved after your approval, so nothing "
            "was merged or pushed",
        )

    def _clean_up(self, pipeline: Pipeline) -> None:
        """Remove the worktree after the merge; the run branch stays. A failure is logged:
        the merge stands, and the clean-up of finished runs removes it after 7 days."""
        places = pipeline.places
        try:
            remove_worktree(pipeline.git, places.repo, places.worktree)
            result = GateResult(
                gate=CLEANUP_GATE,
                passed=True,
                reason=f"removed the worktree {places.worktree}; the run branch stays.",
            )
        except GitError as err:
            result = GateResult(
                gate=CLEANUP_GATE,
                passed=False,
                reason=f"the pull request is merged, but the worktree {places.worktree} wasn't "
                f"removed ({err}). Remove it with `git worktree remove`, or leave it to the "
                "clean-up of finished runs after 7 days.",
            )
        pipeline.events.append(result)


# ------------------------------------------------------------------ shared steps


def _before_rebase(pipeline: Pipeline) -> str | None:
    """What stops the engine from rebasing: changes not committed, or a test lock that fails
    already. None when the run branch can be rebased."""
    git, places = pipeline.git, pipeline.places
    changes = worktree_changes(git, places.worktree)
    if changes:
        return (
            f"the worktree has changes that aren't committed ({_listed(changes)}), so it isn't "
            "the run branch's head."
        )
    changed, deleted = check_lock(git, places.worktree, load_tester_files(places.run_folder))
    if changed or deleted:
        return (
            f"the test lock fails before the rebase: {_listed([*changed, *deleted])}, written by "
            "the tester, changed or gone."
        )
    return None


def _retest(pipeline: Pipeline, gate: str, context: str) -> str | Run:
    """After a rebase the engine finished without conflicts: let the test lock follow main's
    changes to the tester's files, run the builder gate on HEAD, and check the tests left the
    worktree exactly HEAD. Returns the tested head, or the run, stopped for you."""
    git, places = pipeline.git, pipeline.places
    worktree = places.worktree
    try:
        rebased = rebase_tester_files(git, places.run_folder, worktree)
        if rebased:
            pipeline.events.append(lock_rebased(rebased), role="builder")
        head = git.out(["rev-parse", "HEAD"], worktree)
        before = worktree_changes(git, worktree)
        if before:
            raise GateError(f"the worktree isn't its head: {_listed(before)} differ")
        outcome = pipeline.build_gate(head)
        after = worktree_changes(git, worktree)
        now = git.out(["rev-parse", "HEAD"], worktree)
    except (ConfigError, GateError, GitError) as err:
        return pipeline.stop_for_you(None, f"the tests couldn't rerun {context}: {err}")
    pipeline.events.append(
        GateResult(gate=gate, passed=outcome.passed, reason=outcome.reason), role="builder"
    )
    if not outcome.passed:
        return pipeline.stop_for_you(None, f"the tests fail {context}: {outcome.reason}")
    if after or now != head:
        changed = _listed(after) if after else f"HEAD moved to {now}"
        return pipeline.stop_for_you(
            None,
            f"the tests passed {context}, but the test run changed the worktree ({changed}), so "
            f"what passed isn't exactly the commit {head}. Make the tests leave the worktree "
            "as it is, or ignore what they write in .gitignore.",
        )
    return head


def _require_tested(pipeline: Pipeline, head: str) -> None:
    """The worktree must be exactly `head`, the commit whose tests passed."""
    git, worktree = pipeline.git, pipeline.places.worktree
    now = git.out(["rev-parse", "HEAD"], worktree)
    if now != head:
        raise MergeError(f"the worktree's head {now} isn't the pull request's tested head {head}")
    changes = worktree_changes(git, worktree)
    if changes:
        raise MergeError(
            f"the worktree has changes that aren't in the tested head {head}: {_listed(changes)}"
        )


def _listed(items: Sequence[str]) -> str:
    shown = ", ".join(items[:10])
    return f"{shown} and {len(items) - 10} more" if len(items) > 10 else shown


def _still_rebasing(pipeline: Pipeline, run: Run) -> str:
    """Give up a rebase a failure left waiting; what to tell you about it."""
    git, worktree = pipeline.git, pipeline.places.worktree
    try:
        if rebase_in_progress(git, worktree, run_id=run.id, slug=run.slug):
            rebase_abort(git, worktree, run_id=run.id, slug=run.slug)
            return " The rebase was given up."
    except GitError as err:
        return f" A rebase may still wait in {worktree} ({err}); run `git rebase --abort`."
    return ""
