# agent-factory

A local system that turns a request (new project, feature or spec) into a reviewed, tested merge request. Claude Code agents do the work; a deterministic Python engine controls the order, the gates and the retries.

- Design: [docs/design/design.md](docs/design/design.md). This is the source of truth.
- Tasks: [docs/task/task.md](docs/task/task.md).

## Working rules

1. **Follow the design.** Build what `docs/design/design.md` says. If a task seems to need something different, stop and ask. Don't silently deviate. Record any agreed deviation in the milestone summary.
2. **Take the next task.** Work on the first unchecked task in `docs/task/task.md` whose dependencies are all checked. One task at a time; don't start the next one in the same commit.
3. **Tests first.** Write the task's tests, run them and see them fail for the right reason. Then write the code. Before calling a task done, run `uv run tools/dev.py test` and `uv run tools/dev.py lint` for the whole repo; both must pass.
4. **Branch, commit and open a pull request per task.**
   - Start each task from an up-to-date `main` on a new branch named `task/<id>-<slug>`, for example `task/2.4-event-stream`.
   - Mark the task `[x]` in `docs/task/task.md` in the same commit as the code. When a milestone changes status, update its row and add a log line in `docs/task/index.md`.
   - Commit message: `<task id>: <title>`, for example `2.4: Event stream`.
   - Push the branch and open a pull request into `main`. The description gives what changed, the acceptance criteria and how each was tested.
   - Never push to or merge into `main` directly. Kunal merges.
5. **Review fixes.** Every pull request gets an automated code review. Kunal passes the review comments on.
   - For each comment, decide whether it is valid. Fix valid ones on the same branch in a new commit, and say why for any you don't fix.
   - Rerun the tests and lint, then push. Don't open a new pull request.
   - Start the next task only after the current pull request is merged, from the updated `main`.
6. **Stop at milestone ends.** After the last task of a milestone, don't start the next one. Write a summary covering:
   - what was built
   - what was tested (commands and results)
   - anything that deviated from the design and why
   - what's left for the manual exit checklist

   Exit tasks (`1.X`, `2.X`, …) are signed off by Kunal, never by an agent.
7. **Expanding later milestones.** Milestones 3–7 are listed as features only. At the start of one, propose its tasks in the same format as milestones 1–2 and wait for approval before building.

## Tech stack

| Part | Stack | From milestone |
| --- | --- | --- |
| Engine, CLI | Python 3.12, uv 0.9 workspace | 1 |
| Agents | `claude-agent-sdk` 0.2.165 (PreToolUse hooks, `resume`, per-agent `env`) | 2 |
| Config and files on disk | `pydantic` 2.14, `PyYAML` 6.0.3 | 2 |
| CLI | `typer` 0.27 | 2 |
| Storage | SQLite (stdlib `sqlite3`, WAL, no ORM); `run.json` and `events.jsonl` per run | 2 |
| Git, GitHub | `git` and `gh` through `subprocess` | 2 |
| Python tests and lint | `pytest` 9.1, `ruff` 0.16 (lint and format), `pyright` 1.1.414 (basic mode) | 1 |
| HTTP API | `fastapi` 0.143, `uvicorn` 0.54, SSE for live events (`sse-starlette` 3.5), localhost only | 5 |
| Plugin MCP server | `mcp` 2.3 (FastMCP), Python | 5 |
| UI | Node 22 LTS, pnpm 10, React 19, TypeScript, Vite, TanStack Query, Vitest and Testing Library, Playwright, ESLint and Prettier | 6 |

- Versions were checked on Oct 9, 2026. Exact versions are pinned in `uv.lock` and, from milestone 6, `ui/pnpm-lock.yaml`.
- Re-check the UI versions at the start of milestone 6.
- Platform: native Windows first. Isolation is `worktree`; a repo with `sandbox_required` is refused on native Windows.

## Repo layout

```
engine/       Python package factory_engine: engine, gates, registry, events, providers, safety, agents; API from milestone 5
  tests/
cli/          Python package: the `factory` command
plugin/       Claude Code plugin. skills/ from milestone 1; commands, MCP server, hooks and status line from milestone 5
ui/           React frontend, own package.json and tests (milestone 6)
templates/    intent.md, plan.md, handoff.md, review.md, report.md
prompts/      default role prompts
tools/        dev.py (setup, test, lint) and its tests
docs/         design/design.md, task/task.md
```

Rules for how the parts depend on each other:

- `cli/` imports `factory_engine` directly (until milestone 5).
- `plugin/` and `ui/` never import engine code; they use the HTTP API only.
- Backend and frontend share no code.

## Commands

All commands run from the repo root. A part is one of `engine`, `cli`, `plugin`, `ui`. Leaving it out runs every part. Parts that don't exist yet are skipped.

| | Whole repo | One part |
| --- | --- | --- |
| Setup | `uv run tools/dev.py setup` | `uv run tools/dev.py setup engine` |
| Test | `uv run tools/dev.py test` | `uv run tools/dev.py test engine` |
| Lint | `uv run tools/dev.py lint` | `uv run tools/dev.py lint engine` |

What each part runs underneath:

| Part | Setup | Test | Lint |
| --- | --- | --- | --- |
| engine | `uv sync` | `uv run pytest engine/tests` | `uv run ruff check engine` + `uv run ruff format --check engine` + `uv run pyright engine` |
| cli | `uv sync` | `uv run pytest cli/tests` | same as engine, on `cli` |
| plugin | `uv sync` | `uv run pytest plugin/tests` | same as engine, on `plugin` |
| ui | `pnpm -C ui install` | `pnpm -C ui test` | `pnpm -C ui lint` |
| tools | — | `uv run pytest tools/tests` (included in `test`) | included in `lint` |

- Live tests call a real model and are skipped by default. Run them with `uv run pytest -m live`. They need `CLAUDE_CODE_OAUTH_TOKEN`.
- Fix formatting with `uv run ruff format .`.

## Coding conventions

**Python**

- Type hints on every function. Code must pass pyright basic mode with no `# type: ignore` unless it carries a reason.
- Use pydantic models for anything read from or written to disk (`factory.yaml`, `run.json`, events). Validate data on the way in, not deep inside the code.
- Use `pathlib.Path` everywhere. Never build paths with strings. Before comparing paths, resolve them (`.resolve()`) and normalize case. Windows paths, drive letters, UNC paths and junctions are normal inputs.
- Run commands with `subprocess.run([...], check=..., timeout=...)` and a list of arguments. Never use `shell=True`. Always pass `encoding="utf-8"` and `errors="replace"`.
- Open text files with `encoding="utf-8"`. Write state files atomically: write a temp file, then `os.replace`.
- No global mutable state. Pass settings, clock and runner in as arguments so tests can replace them.
- Errors: raise specific exception classes from `factory_engine.errors`. Messages say what failed and what to do next.
- Logging: use the `logging` module for the engine's own logs. Anything a run does goes in its event stream, not the log.

**Engine rules**

- The engine is deterministic: no model calls except through the `AgentRunner` interface.
- Gates are pure functions where possible: input files and command output in, `GateResult` (passed, reason) out.
- Safety checks fail closed. An exception in a hook or check blocks the action.
- Only the engine runs `git push`, `rebase`, `merge` or `gh`. No code path merges into `main` without an approve action.
- Agents get only their provider profile's credentials and their own `CLAUDE_CONFIG_DIR`. Never pass the parent environment through.

**Tests**

- Test files are named `test_<module>.py` and mirror the package layout.
- One behaviour per test. Table-driven (`pytest.mark.parametrize`) for rules such as safety, gates and state transitions.
- Use `FakeAgentRunner`, temporary git repos (`tmp_path`) and a fake `gh` on `PATH`. No network and no real model in default tests.
- Mark real-model tests `@pytest.mark.live`.

**Docs and prompts**

- Templates and run docs stay at most one page.
- The headings in `templates/` are a contract with the gates. Changing one means changing its gate and tests in the same commit.
- Each skill needs a `SKILL.md` with `name` (matching its folder) and a `description` saying when to use it.

**Git**

- One branch and one pull request per task (see working rules 4 and 5). Never commit directly to `main`.
- Never commit secrets, tokens or `.env*` files.

## Things to watch for

- A leftover `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or `ANTHROPIC_BASE_URL` silently switches an agent's provider. The agent environment is always built from a clean base.
- Skills advise; hooks and gates enforce. A rule that must always hold needs a hook or a gate, not just a prompt line.
- Tester-written tests are locked from the builder. Never weaken or delete a test to make a build pass.
