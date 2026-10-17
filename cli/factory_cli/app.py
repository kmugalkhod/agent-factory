"""The `factory` command (Typer): start a run, see its status, decide on it, stop, resume and
attach.

Every command takes the repo from the current folder unless `--repo` names it, and calls one
engine action (`actions.EngineActions`). Commands that drive a run (`start`, `approve`,
`resume`) run the pipeline in the foreground until the run needs you or finishes, printing a
progress line per meaningful event as it happens (`progress.py`; `--verbose` adds tool calls
and agent messages), then where the run stopped.

Exit codes: 0 done, 2 the run needs you (it waits for a decision, needs you, is paused or was
interrupted), 1 an error. An engine error prints its message, never a traceback; a usage error
exits 1 too, so 2 always means a run waits for you.
"""

import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace
from enum import StrEnum
from pathlib import Path
from typing import Annotated, NoReturn, cast

import typer

from factory_cli.actions import Ending, EngineActions, Outcome, RunDetail, RunRow
from factory_cli.progress import Progress
from factory_engine.errors import FactoryError
from factory_engine.events import EventObserver
from factory_engine.pipeline import ROLES, Stop
from factory_engine.run import Role, Run
from factory_engine.states import WORKING

type EngineFactory = Callable[[Path | None, EventObserver | None], EngineActions]
"""Makes the engine actions for a repo (the `--repo` folder, or None for the current folder)
with who is told about the events a driven run writes (None: nobody)."""

NEW_PROJECT_LATER = (
    "Starting a new project (--new-project, the bootstrap run) arrives with task 2.26. For now, "
    "start a feature with --feature or --spec on a repo that has a factory.yaml."
)
FEEDBACK_LATER = (
    "Feedback to an agent arrives with task 2.23. For now, send a waiting plan back with "
    '`factory approve <run> --send-back "<notes>"`, or stop the run and start a new one.'
)
ANSWER_SEPARATOR = "---"
DONE_STATES = ("merged", "closed")
_NEEDS_YOU: tuple[Ending, ...] = (
    "plan_approval",
    "questions",
    "split",
    "needs_you",
    "paused",
    "awaiting_merge",
    "interrupted",
)


class RoleName(StrEnum):
    planner = "planner"
    tester = "tester"
    builder = "builder"
    reviewer = "reviewer"


RepoOption = Annotated[
    Path | None,
    typer.Option(
        "--repo",
        help="A folder in the repo to work on. Default: the current folder.",
        file_okay=False,
    ),
]
RunArgument = Annotated[int, typer.Argument(help="The run's ID, as `factory status` lists it.")]
VerboseOption = Annotated[
    bool,
    typer.Option("--verbose", "-v", help="Also show each tool call and agent message."),
]


def progress(verbose: bool) -> Progress:
    """The progress lines a run-driving command prints while the run works."""
    return Progress(typer.echo, verbose=verbose)


def make_app(engine: EngineFactory) -> typer.Typer:
    """The `factory` command, calling the engine actions `engine` makes for a repo."""
    app = typer.Typer(
        name="factory",
        help="Turn a request into a reviewed, tested pull request.",
        no_args_is_help=True,
        add_completion=False,
        pretty_exceptions_enable=False,
    )

    @app.command()
    def start(
        feature: Annotated[
            str | None, typer.Option(help="What to build, in your words.", show_default=False)
        ] = None,
        spec: Annotated[
            Path | None,
            typer.Option(help="A spec file; copied into intent.md unchanged.", dir_okay=False),
        ] = None,
        new_project: Annotated[
            bool, typer.Option("--new-project", help="Bootstrap a new project (task 2.26).")
        ] = False,
        slug: Annotated[
            str | None, typer.Option(help="The run's short name. Default: from the request.")
        ] = None,
        repo: RepoOption = None,
        verbose: VerboseOption = False,
    ) -> None:
        """Start a run from a feature description or a spec, and run it until it needs you."""
        if new_project:
            fail(NEW_PROJECT_LATER)
        if (feature is None) == (spec is None) or (feature is not None and not feature.strip()):
            fail('Give the request with one of --feature "<what to build>" or --spec <file>.')
        actions = engine(repo, progress(verbose))
        if feature is not None:
            description = feature
            finish(lambda: actions.start_feature(description, slug=slug))
        if spec is not None:
            spec_file = spec
            finish(lambda: actions.start_spec(spec_file, slug=slug))

    @app.command()
    def status(
        run: Annotated[
            int | None, typer.Argument(help="A run's ID for its details.", show_default=False)
        ] = None,
        all_repos: Annotated[bool, typer.Option("--all", help="List every repo's runs.")] = False,
        repo: RepoOption = None,
    ) -> None:
        """List the repo's runs with state, stage, attempts and tokens, or show one run."""
        actions = engine(repo, None)
        if run is not None:
            run_id = run
            detail = call(lambda: actions.describe(run_id))
            echo(detail_lines(detail))
            return
        rows = call(lambda: actions.runs(all_repos=all_repos))
        if not rows:
            echo(['No runs yet. Start one with `factory start --feature "<what to build>"`.'])
            return
        echo(status_table(rows, with_repo=all_repos))

    @app.command()
    def approve(
        run: RunArgument,
        edit: Annotated[
            Path | None,
            typer.Option(help="Your edited plan file: it replaces plan.md, then is approved."),
        ] = None,
        send_back: Annotated[
            str | None,
            typer.Option(help="Send the plan back to the planner with these notes."),
        ] = None,
        answer: Annotated[
            list[str] | None,
            typer.Option(
                "--answer",
                "-a",
                help="An answer to the plan's questions; give one per question, in order. "
                "An empty answer keeps the planner's default.",
            ),
        ] = None,
        answers: Annotated[
            Path | None,
            typer.Option(
                help=f"A file of answers, in order, separated by lines of {ANSWER_SEPARATOR}.",
                dir_okay=False,
            ),
        ] = None,
        repo: RepoOption = None,
        verbose: VerboseOption = False,
    ) -> None:
        """Approve a waiting plan, edit and approve it, send it back, or answer its questions."""
        given = {
            "--edit": edit is not None,
            "--send-back": send_back is not None,
            "--answer": bool(answer),
            "--answers": answers is not None,
        }
        chosen = [name for name, present in given.items() if present]
        if len(chosen) > 1:
            fail(f"Give one decision at a time, not {' and '.join(chosen)}.")
        replies = list(answer or [])
        if answers is not None:
            replies = parse_answers(read_text(answers, "answers"))
        actions = engine(repo, progress(verbose))
        if edit is not None:
            plan_file = edit
            finish(lambda: actions.edit_and_approve(run, plan_file))
        if send_back is not None:
            notes = send_back
            finish(lambda: actions.send_back(run, notes))
        if answer or answers is not None:
            finish(lambda: actions.answer(run, replies))
        finish(lambda: actions.approve(run))

    @app.command()
    def feedback(
        run: RunArgument,
        message: Annotated[str, typer.Argument(help="Your note to the agent.")],
        role: Annotated[RoleName | None, typer.Option(help="The agent to tell.")] = None,
        repo: RepoOption = None,
    ) -> None:
        """Tell an agent something mid-run (arrives with task 2.23)."""
        fail(FEEDBACK_LATER)

    @app.command()
    def stop(run: RunArgument, repo: RepoOption = None) -> None:
        """Stop a run. Its worktree, branch and run folder are kept."""
        actions = engine(repo, None)
        stopped = call(lambda: actions.stop(run))
        echo([f"{label(stopped)}: {stopped.state}. Its worktree and run folder are kept."])

    @app.command()
    def resume(
        run: RunArgument,
        note: Annotated[
            str | None, typer.Option(help="A note for the agent that carries on.")
        ] = None,
        repo: RepoOption = None,
        verbose: VerboseOption = False,
    ) -> None:
        """Carry on a paused or interrupted run (a new attempt of its stage) until it needs you."""
        actions = engine(repo, progress(verbose))
        finish(lambda: actions.resume(run, note=note))

    @app.command()
    def attach(
        run: RunArgument,
        role: Annotated[
            RoleName | None,
            typer.Option(help="The agent whose session to open. Default: the current stage's."),
        ] = None,
        repo: RepoOption = None,
    ) -> None:
        """Print the command that opens an agent's session in a terminal."""
        actions = engine(repo, None)
        chosen = cast(Role, role.value) if role is not None else None
        echo([call(lambda: actions.attach(run, role=chosen))])

    return app


# ------------------------------------------------------------------ running a command


def run_command(app: typer.Typer, args: Sequence[str]) -> int:
    """Run `app` with `args` and return its exit code. A usage error exits 1, not Click's 2,
    which means "needs you" here."""
    command = typer.main.get_command(app)
    try:
        code = command.main(args=list(args), prog_name="factory", standalone_mode=False)
    except typer.TyperException as err:  # a usage error: an unknown option, a missing argument
        show = getattr(err, "show", None)
        if callable(show):
            show()
        else:
            typer.echo(f"error: {err.format_message()}", err=True)
        return 1
    except typer.Abort:
        typer.echo("Aborted.", err=True)
        return 1
    return code if isinstance(code, int) else 0


def main() -> None:
    """The `factory` console script."""
    from factory_cli.engine import Engine, default_deps

    deps = default_deps()

    def engine(repo: Path | None, watch: EventObserver | None) -> Engine:
        return Engine(replace(deps, progress=watch), repo)

    sys.exit(run_command(make_app(engine), sys.argv[1:]))


def fail(message: str) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(1)


def call[T](action: Callable[[], T]) -> T:
    """Run an engine action; an engine error is printed and exits 1."""
    try:
        return action()
    except FactoryError as err:
        fail(str(err))


def finish(action: Callable[[], Outcome]) -> NoReturn:
    """Run a run-driving action, say where the run stopped and exit with its code."""
    done = call(action)
    echo(outcome_lines(done))
    raise typer.Exit(exit_code(done))


def echo(lines: Iterable[str]) -> None:
    for line in lines:
        typer.echo(line)


def read_text(path: Path, what: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        fail(f"{path}: can't read your {what} ({err}). Give the path of a UTF-8 file.")


def parse_answers(text: str) -> list[str]:
    """Answers in order, separated by lines of `---`; each is stripped, and may be empty."""
    answers: list[list[str]] = [[]]
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.strip() == ANSWER_SEPARATOR:
            answers.append([])
        else:
            answers[-1].append(line)
    return ["\n".join(lines).strip() for lines in answers]


# ------------------------------------------------------------------ what the command prints


def exit_code(outcome: Outcome) -> int:
    """0 done, 2 the run needs you, 1 it ended without merging."""
    if outcome.stop in _NEEDS_YOU:
        return 2
    return 0 if outcome.run.state in DONE_STATES else 1


def label(run: Run, *, with_repo: bool = True) -> str:
    return f"run {run.repo}/{run.id} ({run.slug})" if with_repo else f"run {run.id} ({run.slug})"


def outcome_lines(outcome: Outcome) -> list[str]:
    run = outcome.run
    return [
        f"{label(run)}: {run.state}",
        *next_steps(run, outcome.stop, outcome.run_folder, outcome.questions, outcome.reason),
    ]


def next_steps(
    run: Run, stop: Ending | None, folder: Path, questions: Sequence[str], reason: str | None
) -> list[str]:
    """What the run waits for and the commands to go on."""
    n = run.id
    plan = folder / "plan.md"
    if stop == "plan_approval":
        return [
            f"The plan waits for your approval: {plan}",
            f"  approve:          factory approve {n}",
            f"  edit and approve: factory approve {n} --edit <your plan file>",
            f'  send back:        factory approve {n} --send-back "<notes>"',
        ]
    if stop == "questions":
        return [
            f"The planner asks (plan: {plan}):",
            *(f"  Q{i}: {question}" for i, question in enumerate(questions, 1)),
            f'Answer them in order: factory approve {n} --answer "<answer 1>" --answer ...',
            "(an empty answer keeps the planner's default; for long answers use --answers "
            f"<file>, answers separated by lines of {ANSWER_SEPARATOR}). You can also edit and "
            "approve the plan, or send it back with --send-back.",
        ]
    if stop in ("split", "needs_you"):
        return [
            f"It needs you: {reason or 'see the reason in its events.jsonl.'}",
            f"End it with `factory stop {n}`; feedback arrives with task 2.23.",
        ]
    if stop == "paused":
        return [f"It is paused: {reason or run.state}", f"Carry on with `factory resume {n}`."]
    if stop == "awaiting_merge":
        return [
            "The review passed and its pull request is open; it waits for your merge approval.",
            f"Review the pull request, then merge it with `factory approve {n}`.",
        ]
    if stop == "interrupted":
        return [
            f"Stopped by Ctrl-C while {run.state}. Carry on with `factory resume {n}`, or end it "
            f"with `factory stop {n}`."
        ]
    if stop == "ended":
        return ["Done."] if run.state in DONE_STATES else [f"It ended {run.state}."]
    return []


def detail_lines(detail: RunDetail) -> list[str]:
    run = detail.run
    state = _shown_state(run, detail.running)
    lines = [
        f"{label(run)}: {state}",
        f"  kind:       {run.kind}",
        f"  stage:      {run.stage or '-'}",
        f"  attempts:   {_attempts(run)}",
        f"  tokens:     {_tokens(run)}",
        f"  last step:  {run.last_completed_step or '-'}",
        f"  run folder: {detail.run_folder}",
        f"  worktree:   {detail.worktree}",
    ]
    stop = _waiting(run, detail)
    if stop is None and not detail.running and run.state in (*WORKING, "pending"):
        stop = "interrupted"
    return lines + next_steps(run, stop, detail.run_folder, detail.questions, detail.reason)


def status_table(rows: Sequence[RunRow], *, with_repo: bool) -> list[str]:
    header = ("RUN", "STATE", "STAGE", "ATTEMPTS", "TOKENS")
    body = [
        (
            f"{row.run.repo}/{row.run.id}-{row.run.slug}"
            if with_repo
            else f"{row.run.id}-{row.run.slug}",
            _shown_state(row.run, row.running),
            row.run.stage or "-",
            _attempts(row.run),
            _tokens(row.run),
        )
        for row in rows
    ]
    widths = [max(len(line[i]) for line in [header, *body]) for i in range(len(header))]
    return [
        "  ".join(cell.ljust(width) for cell, width in zip(line, widths, strict=True)).rstrip()
        for line in [header, *body]
    ]


def _waiting(run: Run, detail: RunDetail) -> Stop | None:
    if run.state == "awaiting_plan_approval":
        return "questions" if detail.questions else "plan_approval"
    if run.state in ("needs_you", "paused", "awaiting_merge"):
        return cast(Stop, run.state)
    if run.state in ("merged", "failed", "stopped", "closed"):
        return "ended"
    return None


def _shown_state(run: Run, running: bool) -> str:
    if run.state in (*WORKING, "pending") and not running:
        return f"{run.state} (interrupted)"
    return run.state


def _attempts(run: Run) -> str:
    done = [f"{role} {run.attempts[role]}" for role in ROLES if run.attempts.get(role)]
    return ", ".join(done) or "-"


def _tokens(run: Run) -> str:
    return f"{sum(run.metrics.tokens.values()):,}"
