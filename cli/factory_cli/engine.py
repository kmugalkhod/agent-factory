"""The real engine actions behind the `factory` command (task 2.28).

Each action works on one repo: the git repo around the current folder, or the one `--repo`
names. It reads the settings (the built-in defaults, `<data>/settings.yaml` if present, then the
repo's `factory.yaml`), checks the provider (token present, expiry recorded and not near),
checks the readiness bar before a new run (`factory_engine.readiness`; a repo that fails it
gets no run, and every failing condition is listed) and builds the engine's objects: the
registry, the run's event log, intake, the decisions and a `Pipeline` with the real agent
runner (the watchdog lives inside the pipeline). It runs the pipeline in the foreground until
the run waits for you or stops; the run's event log tells `Deps.progress` about each event as
it is written, so the command can print progress lines.

A process holds the run's lock (`lock.py`) while it decides on a run or drives it, so two
factory processes never write the same run. Ctrl-C or a crash stops the pipeline; the run stays
in the state it was in, and `resume` carries it on from its last completed step
(`Pipeline.recover`, task 2.24). A paused run instead carries on the attempt it paused in, in
the same session (`Pipeline.resume_paused`).
"""

import asyncio
import os
import re
import shlex
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from factory_cli.actions import Outcome, RunDetail, RunRow
from factory_cli.errors import CliError
from factory_cli.lock import RunLock
from factory_engine import decisions
from factory_engine.agent_config import agent_config_dir
from factory_engine.agents.runner import AgentRunner, ClaudeAgentRunner, persist
from factory_engine.agents.watchdog import Sleep
from factory_engine.config import (
    Settings,
    load_global_settings,
    load_repo_config,
    merge_settings,
)
from factory_engine.decisions import Decided, DecisionContext
from factory_engine.errors import DataFolderError, FactoryError
from factory_engine.events import EventLog, EventObserver, GateResult, NeedsYou, read_events
from factory_engine.gates.plan import PLAN_FILE, Question, check_plan
from factory_engine.git_ops import Git, worktree_dir
from factory_engine.github import GitHub
from factory_engine.intake import Intake, Started
from factory_engine.merge import GitFlow
from factory_engine.paths import WINDOWS_DEVICE, data_dir, run_dir
from factory_engine.pipeline import ROLES, Pipeline, PipelineResult, Places
from factory_engine.providers import Profile, get_profile, preflight
from factory_engine.readiness import ReadinessCheck, check_readiness
from factory_engine.registry import Registry, open_registry, registry_path
from factory_engine.report import REPORT_GATE, write_report
from factory_engine.run import Kind, Role, Run, RunState, load_run
from factory_engine.states import WORKING, move

GLOBAL_SETTINGS = "settings.yaml"
"""The global settings file in the data folder (optional; `token_expires` lives there)."""
REPO_CONFIG = "factory.yaml"
LOCK_FILE = "engine.lock"
READINESS = "readiness"
"""The data folder's subfolder for the readiness check's temporary checkouts."""
HOME_ENV = "AGENT_FACTORY_HOME"
"""Overrides the factory's own folder (with `prompts/` and `plugin/skills/`)."""
CLEARED_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
"""Variables an attached session must not see: each would switch its provider."""
RESUMABLE: tuple[RunState, ...] = ("pending", *WORKING)
ENDED: tuple[RunState, ...] = ("merged", "failed", "stopped", "closed")
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")
_NOT_NAME = re.compile(r"[^A-Za-z0-9._-]+")

type RunnerFactory = Callable[[Mapping[str, str]], AgentRunner]
type Act = Callable[[DecisionContext, Run], Decided]


@dataclass(frozen=True)
class Deps:
    """What the actions use from the outside world, passed in so tests can replace it: the
    environment, the current folder, a clock and a sleep, the platform (`sys.platform`), the
    factory's own folder, the agent runner to use and the readiness check. `progress`, if
    given, is told about each event a run writes while this process works on it (the command's
    progress lines)."""

    env: Mapping[str, str]
    cwd: Path
    clock: Callable[[], datetime]
    sleep: Sleep
    platform: str
    home: Path
    runner: RunnerFactory
    progress: EventObserver | None = None
    readiness: ReadinessCheck = check_readiness


def default_deps() -> Deps:
    """The real world: `os.environ`, the current folder, the system clock, the Agent SDK."""
    env = os.environ
    return Deps(
        env=env,
        cwd=Path.cwd(),
        clock=lambda: datetime.now(UTC),
        sleep=asyncio.sleep,
        platform=sys.platform,
        home=factory_home(env),
        runner=lambda parent: ClaudeAgentRunner(parent),
    )


def factory_home(env: Mapping[str, str]) -> Path:
    """`$AGENT_FACTORY_HOME`, else the agent-factory checkout this package is installed from."""
    override = env.get(HOME_ENV, "").strip()
    return Path(override) if override else Path(__file__).resolve().parents[2]


class Engine:
    """The engine actions on one repo: `repo` (a folder in it), else the current folder."""

    def __init__(self, deps: Deps, repo: Path | None) -> None:
        self._deps = deps
        self._repo_arg = repo
        self._git = Git(deps.env)

    # ------------------------------------------------------------------ start

    def start_feature(self, description: str, *, slug: str | None) -> Outcome:
        return self._start(
            lambda intake, who: intake.feature(description, requested_by=who, slug=slug)
        )

    def start_spec(self, path: Path, *, slug: str | None) -> Outcome:
        spec = path if path.is_absolute() else self._deps.cwd / path
        return self._start(lambda intake, who: intake.spec(spec, requested_by=who, slug=slug))

    def _start(self, make: Callable[[Intake, str], Started]) -> Outcome:
        with self._registry() as registry:
            repo, path = self._repo(registry)
            # No factory.yaml yet: the readiness check names it with everything else missing.
            settings = self._settings(path, "feature", required=False)
            profile = self._preflight(settings)
            self._deps.readiness(
                path,
                settings,
                git=self._git,
                github=GitHub(env=self._deps.env),
                parent_env=self._deps.env,
                platform=self._deps.platform,
                scratch=self._data() / READINESS,
            ).require(path)
            if registry.get_repo(repo) is None:  # after the checks: a refused start adds nothing
                registry.add_repo(repo, path, now=self._deps.clock())
            held: list[RunLock] = []

            def lock(folder: Path) -> None:
                # Taken as soon as the run's folder is claimed, before intake publishes the
                # run, and held until the pipeline stops: no other process can stop or decide
                # the run in between.
                run_lock = RunLock(folder / LOCK_FILE)
                run_lock.acquire()
                held.append(run_lock)

            intake = Intake(
                data=self._data(),
                repo=repo,
                repo_path=path,
                settings=settings,
                git=self._git,
                registry=registry,
                clock=self._deps.clock,
                on_claim=lock,
                observer=self._deps.progress,
            )
            try:
                started = make(intake, self._requested_by(path))
                return self._drive(
                    started.run,
                    started.run_folder,
                    repo_path=path,
                    settings=settings,
                    profile=profile,
                    registry=registry,
                    events=started.events,
                    note=None,
                )
            finally:
                for run_lock in held:
                    run_lock.release()

    # ------------------------------------------------------------------ decisions

    def approve(self, run_id: int) -> Outcome:
        def act(ctx: DecisionContext, run: Run) -> Decided:
            if run.state == "awaiting_merge":
                # The run stays in awaiting_merge; `_drive` then advances a pipeline built by
                # `_pipeline` (with the GitFlow hooks), which merges or needs you.
                return decisions.approve_merge(ctx)
            return decisions.approve_plan(ctx)

        return self._decide(run_id, act)

    def edit_and_approve(self, run_id: int, plan_file: Path) -> Outcome:
        path = plan_file if plan_file.is_absolute() else self._deps.cwd / plan_file
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as err:
            raise CliError(
                f"{path}: can't read your edited plan ({err}). Give the path of a UTF-8 file."
            ) from err
        return self._decide(run_id, lambda ctx, _run: decisions.edit_and_approve(ctx, text))

    def send_back(self, run_id: int, notes: str) -> Outcome:
        return self._decide(run_id, lambda ctx, _run: decisions.send_back(ctx, notes))

    def answer(self, run_id: int, answers: Sequence[str]) -> Outcome:
        given = list(answers)
        return self._decide(run_id, lambda ctx, _run: decisions.answer_questions(ctx, given))

    def _decide(self, run_id: int, act: Act) -> Outcome:
        """Apply a decision to a waiting run, then carry the run on from where it moved."""
        with self._registry() as registry:
            repo, path = self._repo(registry)
            folder = self._folder(registry, repo, run_id)
            with RunLock(folder / LOCK_FILE):
                run = load_run(folder)
                settings = self._settings(path, run.kind)
                events = self._events(run, folder)
                ctx = self._context(folder, events, registry)
                profile = self._preflight(settings)  # before deciding: nothing half done
                decided = act(ctx, run)
                return self._drive(
                    decided.run,
                    folder,
                    repo_path=path,
                    settings=settings,
                    profile=profile,
                    registry=registry,
                    events=events,
                    note=decided.note,
                )

    # ------------------------------------------------------------------ stop and resume

    def stop(self, run_id: int) -> Run:
        with self._registry() as registry:
            repo, path = self._repo(registry)
            folder = self._folder(registry, repo, run_id)
            with RunLock(folder / LOCK_FILE):
                run = load_run(folder)
                if run.state in ENDED:
                    raise CliError(
                        f"run {run.repo}/{run.id} has already ended ({run.state}); there is "
                        "nothing to stop."
                    )
                stopped = move(run, "stopped", log=self._events(run, folder))
                stopped = stopped.model_copy(update={"pause": None})
                persist(stopped, folder, registry)
                self._report(stopped, folder, path)
                return stopped

    def _report(self, run: Run, folder: Path, repo_path: Path) -> None:
        """The stopped run's `report.md`, with the models when the repo's settings load. A
        report that can't be written is logged; the stop stands."""
        try:
            settings: Settings | None = self._settings(repo_path, run.kind)
        except FactoryError:
            settings = None
        try:
            profile = get_profile(run.provider)
        except FactoryError:
            profile = None
        try:
            write_report(run, folder, settings=settings, profile=profile)
        except FactoryError as err:
            self._events(run, folder).append(
                GateResult(gate=REPORT_GATE, passed=False, reason=str(err))
            )

    def resume(self, run_id: int, *, note: str | None) -> Outcome:
        with self._registry() as registry:
            repo, path = self._repo(registry)
            folder = self._folder(registry, repo, run_id)
            with RunLock(folder / LOCK_FILE):
                run = load_run(folder)
                if run.state != "paused" and run.state not in RESUMABLE:
                    raise CliError(_not_resumable(run, folder))
                settings = self._settings(path, run.kind)
                profile = self._preflight(settings)
                return self._drive(
                    run,
                    folder,
                    repo_path=path,
                    settings=settings,
                    profile=profile,
                    registry=registry,
                    events=self._events(run, folder),
                    note=note,
                    # A paused run carries on the attempt it paused in, in the same session;
                    # an interrupted one from its last completed step.
                    continue_paused=run.state == "paused",
                    recover=run.state != "paused",
                )

    # ------------------------------------------------------------------ status and attach

    def runs(self, *, all_repos: bool) -> list[RunRow]:
        with self._registry() as registry:
            if all_repos:
                found = registry.list_runs()
            else:
                repo, _ = self._repo(registry)
                found = registry.list_runs(repo)
            return [RunRow(run, self._running(run)) for run in found]

    def describe(self, run_id: int) -> RunDetail:
        with self._registry() as registry:
            repo, _ = self._repo(registry)
            folder = self._folder(registry, repo, run_id)
            run = load_run(folder)
            questions: tuple[str, ...] = ()
            if run.state == "awaiting_plan_approval":
                questions = _questions(check_plan(folder / PLAN_FILE).questions)
            return RunDetail(
                run=run,
                running=self._running(run),
                run_folder=folder,
                worktree=worktree_dir(self._data(), run.repo, run.id, run.slug),
                questions=questions,
                reason=_reason(run, folder),
            )

    def attach(self, run_id: int, *, role: Role | None) -> str:
        with self._registry() as registry:
            repo, _ = self._repo(registry)
            folder = self._folder(registry, repo, run_id)
            run = load_run(folder)
        chosen: Role | None = (
            role or run.stage or next((r for r in ROLES if r in run.session_ids), None)
        )
        if chosen is None:
            raise CliError(
                f"no agent has started on run {run.repo}/{run.id} yet, so there is no saved "
                "session to attach to."
            )
        session = run.session_ids.get(chosen)
        if session is None:
            raise CliError(
                f"the {chosen} of run {run.repo}/{run.id} has no saved session, so there is "
                "nothing to attach to. Pick a role that has run with --role."
            )
        if not _SESSION_ID.fullmatch(session):
            raise CliError(
                f"the {chosen}'s saved session ID {session!r} isn't one the factory writes. "
                "Check the run's run.json."
            )
        data = self._data()
        config = agent_config_dir(data, run.repo, run.id, run.slug, chosen)
        worktree = worktree_dir(data, run.repo, run.id, run.slug)
        lines = []
        if self._running(run):
            lines.append(
                "A factory process is driving this run now. Press Ctrl-C there first, so two "
                "sessions don't work in the worktree at once."
            )
        lines.append(
            f"Resume the {chosen}'s session (run {run.repo}/{run.id}) in a new terminal. It "
            "runs without the engine's safety hook; its config folder's rules still apply, and "
            "the engine checks what changed when the run carries on."
        )
        lines.append(_attach_command(self._deps.platform, worktree, config, session))
        return "\n".join(lines)

    # ------------------------------------------------------------------ the pipeline

    def _drive(
        self,
        run: Run,
        folder: Path,
        *,
        repo_path: Path,
        settings: Settings,
        profile: Profile,
        registry: Registry,
        events: EventLog,
        note: str | None,
        continue_paused: bool = False,
        recover: bool = False,
    ) -> Outcome:
        """Run the pipeline in the foreground until the run waits for you or stops."""
        pipeline = self._pipeline(
            run,
            folder,
            repo_path=repo_path,
            settings=settings,
            profile=profile,
            registry=registry,
            events=events,
        )
        try:
            if continue_paused:
                result = asyncio.run(pipeline.resume_paused(note=note))
            elif recover:
                result = asyncio.run(pipeline.recover(note=note))
            else:
                result = asyncio.run(pipeline.advance(note=note))
        except KeyboardInterrupt:
            return Outcome(load_run(folder), "interrupted", folder)
        result = decisions.settle(self._context(folder, events, registry), result)
        if result.run.state != pipeline.run.state:  # a split that needs you: report it too
            pipeline.write_report(result.run)
        return _outcome(result, folder)

    def _pipeline(
        self,
        run: Run,
        folder: Path,
        *,
        repo_path: Path,
        settings: Settings,
        profile: Profile,
        registry: Registry,
        events: EventLog,
    ) -> Pipeline:
        """The one place a `Pipeline` is built, for every command that drives a run."""
        home = self._deps.home
        places = Places(
            repo=repo_path,
            data=self._data(),
            worktree=worktree_dir(self._data(), run.repo, run.id, run.slug),
            run_folder=folder,
            prompts=home / "prompts",
            skills=home / "plugin" / "skills",
        )
        _check_places(places)
        return Pipeline(
            run,
            places=places,
            settings=settings,
            profile=profile,
            parent_env=self._deps.env,
            git=self._git,
            runner=self._deps.runner(self._deps.env),
            events=events,
            registry=registry,
            clock=self._deps.clock,
            sleep=self._deps.sleep,
            # The git flow (2.22): rebase before review, push and open the pull request, and
            # merge only after `approve` records your approval. `gh` runs with your login.
            hooks=GitFlow(GitHub(env=self._deps.env)),
        )

    # ------------------------------------------------------------------ places and settings

    @contextmanager
    def _registry(self) -> Iterator[Registry]:
        with open_registry(registry_path(self._data())) as registry:
            yield registry

    def _data(self) -> Path:
        data = data_dir(self._deps.env)
        try:
            data.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            raise DataFolderError(
                f"{data}: can't create the factory data folder ({err}). Set AGENT_FACTORY_DATA "
                "to a writable folder."
            ) from err
        return data

    def _repo(self, registry: Registry) -> tuple[str, Path]:
        """The repo's name and main checkout. A registered checkout keeps its name; a new one
        is named after its folder."""
        start = self._repo_arg if self._repo_arg is not None else self._deps.cwd
        if not start.is_absolute():
            start = self._deps.cwd / start
        if not start.is_dir():
            raise CliError(f"{start} isn't a folder. Pass --repo with the repo's folder.")
        done = self._git.run(["rev-parse", "--show-toplevel"], start, check=False)
        top = done.stdout.strip()
        if done.returncode != 0 or not top:
            raise CliError(
                f"{start} isn't inside a git repo. Run factory from the repo's folder, or pass "
                "--repo with it."
            )
        path = _main_checkout(self._git, start, Path(top))
        for known in registry.list_repos():
            if _same_path(known.path, path):
                return known.name, path
        name = _repo_name(path)
        taken = registry.get_repo(name)
        if taken is not None:
            raise CliError(
                f"a repo named {name!r} is already registered at {taken.path}, not {path}. "
                "Rename one of the folders, or run factory from the registered one."
            )
        return name, path

    def _settings(self, repo_path: Path, kind: Kind, *, required: bool = True) -> Settings:
        global_file = self._data() / GLOBAL_SETTINGS
        global_settings = load_global_settings(
            global_file if global_file.is_file() else None, self._deps.platform
        )
        repo_file = repo_path / REPO_CONFIG
        if not repo_file.is_file():
            if not required:
                return merge_settings(global_settings, bootstrap=kind == "bootstrap")
            raise CliError(
                f"{repo_file} doesn't exist. Write the repo's factory.yaml and merge it to main; "
                "the run needs its commands."
            )
        return merge_settings(
            global_settings, load_repo_config(repo_file), bootstrap=kind == "bootstrap"
        )

    def _preflight(self, settings: Settings) -> Profile:
        return preflight(settings, self._deps.env, today=self._deps.clock().date())

    def _folder(self, registry: Registry, repo: str, run_id: int) -> Path:
        indexed = registry.get_run(repo, run_id)
        if indexed is None:
            raise CliError(f"repo {repo} has no run {run_id}. `factory status` lists its runs.")
        return run_dir(self._data(), repo, run_id, indexed.slug)

    def _events(self, run: Run, folder: Path) -> EventLog:
        return EventLog(
            folder,
            repo=run.repo,
            run_id=run.id,
            clock=self._deps.clock,
            observer=self._deps.progress,
        )

    def _context(self, folder: Path, events: EventLog, registry: Registry) -> DecisionContext:
        return DecisionContext(
            run_folder=folder, events=events, registry=registry, clock=self._deps.clock
        )

    def _running(self, run: Run) -> bool:
        folder = run_dir(self._data(), run.repo, run.id, run.slug)
        return RunLock(folder / LOCK_FILE).held()

    def _requested_by(self, repo_path: Path) -> str:
        """Your git user name in the repo, else your login name."""
        done = self._git.run(["config", "user.name"], repo_path, check=False)
        lines = done.stdout.strip().splitlines() if done.returncode == 0 else []
        if lines and lines[0].strip():
            return lines[0].strip()
        env = {key.upper(): value for key, value in self._deps.env.items()}
        return (env.get("USERNAME") or env.get("USER") or "you").strip() or "you"


# ------------------------------------------------------------------ helpers


def _outcome(result: PipelineResult, folder: Path) -> Outcome:
    questions: tuple[str, ...] = ()
    if result.stop == "questions" and result.plan is not None:
        questions = _questions(result.plan.questions)
    return Outcome(
        result.run, result.stop, folder, questions=questions, reason=_reason(result.run, folder)
    )


def _questions(asked: Sequence[Question] | None) -> tuple[str, ...]:
    return tuple(f"{q.text} (default: {q.default})" if q.default else q.text for q in (asked or ()))


def _reason(run: Run, folder: Path) -> str | None:
    """Why the run needs you (its last `needs_you` event) or is paused."""
    if run.state == "paused" and run.pause is not None:
        pause = run.pause
        until = f" It resumes after {pause.resume_at:%Y-%m-%d %H:%M %Z}." if pause.resume_at else ""
        return f"{pause.detail}{until}"
    if run.state != "needs_you":
        return None
    reasons = [e.body.reason for e in read_events(folder) if isinstance(e.body, NeedsYou)]
    return reasons[-1] if reasons else None


def _not_resumable(run: Run, folder: Path) -> str:
    label = f"run {run.repo}/{run.id}"
    if run.state == "awaiting_plan_approval":
        return (
            f"{label} waits for your decision on its plan, not a resume. Decide with "
            f"`factory approve {run.id}` (or --edit, --send-back, --answer)."
        )
    if run.state == "awaiting_merge":
        return (
            f"{label} waits for your merge approval, not a resume. Review its pull request, "
            f"then merge it with `factory approve {run.id}`."
        )
    if run.state == "needs_you":
        why = _reason(run, folder) or "see its events.jsonl"
        return (
            f"{label} needs you: {why} Resuming would rerun the stage without the engine "
            "knowing what changed; feedback that does this safely arrives with task 2.23. "
            f"For now, stop it with `factory stop {run.id}` and start a new run."
        )
    return f"{label} has ended ({run.state}); start a new run instead."


def _check_places(places: Places) -> None:
    if not places.prompts.is_dir() or not places.skills.is_dir():
        raise CliError(
            f"{places.prompts.parent} has no prompts/ or plugin/skills/ folder, so it isn't the "
            f"agent-factory folder. Set {HOME_ENV} to the agent-factory checkout."
        )
    if not places.worktree.is_dir():
        raise CliError(
            f"the run's worktree {places.worktree} is missing, so the run can't carry on. "
            "Stop it with `factory stop` and start a new run."
        )


def _attach_command(platform: str, worktree: Path, config: Path, session: str) -> str:
    if platform == "win32":
        cleared = ", ".join(f"Env:{name}" for name in CLEARED_VARS)
        return (
            f"Set-Location -LiteralPath {_ps_quote(str(worktree))}; "
            f"Remove-Item {cleared} -ErrorAction SilentlyContinue; "
            f"$env:CLAUDE_CONFIG_DIR = {_ps_quote(str(config))}; "
            f"claude --resume {session}"
        )
    cleared = " ".join(f"-u {name}" for name in CLEARED_VARS)
    return (
        f"cd {shlex.quote(str(worktree))} && env {cleared} "
        f"CLAUDE_CONFIG_DIR={shlex.quote(str(config))} claude --resume {session}"
    )


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _main_checkout(git: Git, start: Path, top: Path) -> Path:
    """The repo's main checkout. From a linked worktree (a run's), that is the folder holding
    the shared git folder; otherwise the top of the checkout `start` is in."""
    done = git.run(["rev-parse", "--path-format=absolute", "--git-common-dir"], start, check=False)
    common = done.stdout.strip()
    if done.returncode == 0 and common:
        shared = Path(common).resolve()
        if shared.name.lower() == ".git":
            return shared.parent
    return top.resolve()


def _repo_name(path: Path) -> str:
    name = _NOT_NAME.sub("-", path.name).strip("-.")[:100].rstrip(".") or "repo"
    return f"{name}-repo" if WINDOWS_DEVICE.fullmatch(name) else name


def _same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))
