"""Update before review, push, pull request and merge (design: "Git flow", steps 3 to 7).

`GitFlow` is the pipeline's `Hooks` for a repo with a remote on GitHub:

- `update`, in `updating` before the review: fetch main and rebase the run branch onto it.
  - No conflicts: the builder gate reruns the tests on the result (the test lock included). A
    pass goes on to the review; a fail needs you.
  - Conflicts: the builder resumes, told the conflicted files (→ building), if it has an
    attempt left; otherwise the rebase is given up and the run needs you. How the rebase
    carries on after the builder's edits is in `pipeline.py`.
- `publish`, once the review passed: push the run branch (the safe force-push) and open the
  pull request, or move an open one to the new head. Its number, URL and head go to `run.json`.
- `merge`, in `awaiting_merge` once your approval is recorded (`decisions.approve_merge`): if
  main moved since the pull request's head was tested, rebase onto it, rerun the tests and push
  first. Then merge through `gh`, pinned to the tested head, and remove the worktree (→ merged).
  Anything that fails stops the run for you, with nothing merged.

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
from factory_engine.gates.plan import PLAN_FILE
from factory_engine.git_ops import (
    main_moved,
    push_run_branch,
    rebase_abort,
    rebase_in_progress,
    rebase_onto_main,
    remove_worktree,
    run_branch,
)
from factory_engine.github import GitHub, MergeApproval, PullRequest, merge_pr, open_pr, pr_body
from factory_engine.pipeline import REVIEW_FILE, UPDATE_GATE, Hooks, Pipeline, conflict_message
from factory_engine.run import PullRequestInfo, Run

MERGE_UPDATE_GATE = "merge.update"
"""The `gate_result` of a rebase onto a main that moved after your approval, and its tests."""
CLEANUP_GATE = "merge.cleanup"
"""The `gate_result` of removing the worktree after the merge."""
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
            step = rebase_onto_main(
                git, worktree, run_id=run.id, slug=run.slug, remote=self.remote, base=self.base
            )
        except GitError as err:
            return pipeline.stop_for_you(None, f"the rebase onto {self.base} couldn't run: {err}")
        if step.conflicts:
            return self._conflicts(pipeline, run, step.conflicts)
        try:
            outcome = pipeline.build_gate(git.out(["rev-parse", "HEAD"], worktree))
        except (ConfigError, GateError, GitError) as err:
            return pipeline.stop_for_you(
                None, f"the tests couldn't rerun after the rebase onto {self.base}: {err}"
            )
        pipeline.events.append(
            GateResult(gate=UPDATE_GATE, passed=outcome.passed, reason=outcome.reason),
            role="builder",
        )
        if not outcome.passed:
            return pipeline.stop_for_you(
                None, f"the tests fail after the rebase onto {self.base}: {outcome.reason}"
            )
        return pipeline.run

    def _conflicts(self, pipeline: Pipeline, run: Run, files: Sequence[str]) -> Run:
        listed = ", ".join(files)
        if pipeline.attempts_left("builder") == 0:
            try:
                rebase_abort(pipeline.git, pipeline.places.worktree, run_id=run.id, slug=run.slug)
                undone = "The rebase was given up, so the run branch is as it was."
            except GitError as err:
                undone = f"The rebase couldn't be given up ({err}); run `git rebase --abort`."
            return pipeline.stop_for_you(
                None,
                f"the rebase onto {self.base} stopped on conflicts in {listed}, and the builder "
                f"has used all its attempts, so nobody can resolve them. {undone}",
            )
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
            push_run_branch(
                git,
                places.worktree,
                run_id=run.id,
                slug=run.slug,
                branch=run_branch(run.id, run.slug),
                remote=self.remote,
            )
            head = git.out(["rev-parse", "HEAD"], places.worktree)
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
        places, git = pipeline.places, pipeline.git
        run = pipeline.run
        try:
            check_approval(run, places.run_folder)
            pr = run.pull_request
            if pr is None:
                raise MergeError("no pull request is recorded")
            head = git.out(["rev-parse", "HEAD"], places.worktree)
            if head != pr.head:
                raise MergeError(
                    f"the worktree's head {head} isn't the pull request's tested head {pr.head}"
                )
            moved = main_moved(git, places.worktree, remote=self.remote, base=self.base)
        except FactoryError as err:
            return pipeline.stop_for_you(None, f"nothing was merged: {err}")
        if moved:
            run = self._rebase_for_merge(pipeline, run, pr)
            if run.state != "awaiting_merge":
                return run
        try:
            merge_approved(self.gh, places.repo, run, places.run_folder)
        except FactoryError as err:
            return pipeline.stop_for_you(None, f"the merge failed: {err}")
        pipeline.record(run.model_copy(update={"merge_approval": None}))
        pipeline.move_to("merged")
        self._clean_up(pipeline)
        return pipeline.run

    def _rebase_for_merge(self, pipeline: Pipeline, run: Run, pr: PullRequestInfo) -> Run:
        """Main moved after your approval: rebase onto it, rerun the tests and push, so what
        merges is tested. Returns the run, stopped for you if any of it failed."""
        places, git = pipeline.places, pipeline.git
        worktree = places.worktree
        try:
            step = rebase_onto_main(
                git, worktree, run_id=run.id, slug=run.slug, remote=self.remote, base=self.base
            )
            if step.conflicts:
                rebase_abort(git, worktree, run_id=run.id, slug=run.slug)
                return pipeline.stop_for_you(
                    None,
                    f"{self.base} moved after your approval, and the rebase onto it stopped on "
                    f"conflicts in {', '.join(step.conflicts)}. The rebase was given up and "
                    f"nothing was merged; pull request #{pr.number} is as you approved it.",
                )
            head = git.out(["rev-parse", "HEAD"], worktree)
            outcome = pipeline.build_gate(head)
        except (ConfigError, GateError, GitError) as err:
            return pipeline.stop_for_you(
                None,
                f"{self.base} moved after your approval, and the rebase onto it and the tests "
                f"couldn't run, so nothing was merged: {err}{_still_rebasing(pipeline, run)}",
            )
        pipeline.events.append(
            GateResult(gate=MERGE_UPDATE_GATE, passed=outcome.passed, reason=outcome.reason),
            role="builder",
        )
        if not outcome.passed:
            return pipeline.stop_for_you(
                None,
                f"{self.base} moved after your approval, and the tests fail after the rebase "
                f"onto it, so nothing was merged or pushed: {outcome.reason}",
            )
        try:
            push_run_branch(
                git,
                worktree,
                run_id=run.id,
                slug=run.slug,
                branch=run_branch(run.id, run.slug),
                remote=self.remote,
            )
        except GitError as err:
            return pipeline.stop_for_you(
                None, f"the rebased run branch couldn't be pushed, so nothing was merged: {err}"
            )
        approved = run.merge_approval.head if run.merge_approval is not None else pr.head
        info = pr.model_copy(update={"head": head, "rebased_from": approved})
        return pipeline.record(run.model_copy(update={"pull_request": info}))

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
