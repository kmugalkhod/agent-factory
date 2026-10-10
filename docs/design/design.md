# Software Factory — Design

Oct 9, 2026 · @KUNAL

## Summary

The factory is a local system that turns a request (a new project, a new feature or a written spec) into a reviewed, tested merge request. Claude Code agents do the work; a deterministic Python engine controls the order, the checks and the retries. You decide at four points: the plan, any split, questions the planner raises, and the merge.

**Version 1 scope**

- Runs on your own Windows machine, on your own repos.
- One pilot: your new project, starting with a bootstrap run.
- Models from your Claude subscription by default; LiteLLM on Bedrock available as a configurable provider.
- Same GitHub account for agents and you; the engine does all pushing, rebasing and merging.
- Interfaces: Claude Code plugin, CLI, Windows Terminal panes, then a React UI. All use one engine.

**Version 1 success target:** on the pilot repo, at least 6 of 10 real tasks merged with only small edits from you.

The design follows Anthropic's AI-native SDLC playbook: every stage writes a file the next stage reads, checks run as code rather than prompts, and humans decide at gates rather than watching every step.

## Architecture

Claude Code supplies the intelligence; a deterministic Python engine supplies the control. Agents decide how to do a step; the engine decides which step runs, whether it passed, and what happens next.

&#91;embedded content: factory architecture · interfaces, engine, repos\]

Every interface talks to the one engine, the engine runs each stage agent in that run's worktree, and only the engine talks to GitHub.

| Part                                                          | Built with                                         | Uses a model                 |
| ------------------------------------------------------------- | -------------------------------------------------- | ---------------------------- |
| Stage agents (planner, tester, builder, reviewer, onboarding) | Claude Code through the Claude Agent SDK, Python   | Yes                          |
| Engine: stages, gates, queue, registry, events                | Plain Python                                       | No, deterministic on purpose |
| Orchestrator                                                  | Any Claude Code session with the factory plugin    | Yes                          |
| API server for UI and panes                                   | FastAPI with live event push                       | No                           |
| UI                                                            | React                                              | No                           |
| Storage                                                       | Files per run, SQLite for registry and index       | No                           |
| Isolation                                                     | Git worktrees; Claude Code sandbox where available | No                           |

**From Claude Code, without building anything:** tools, sessions and resume, subagents, per-role settings and permissions, hooks, the sandbox, `CLAUDE.md`, skills, `.claude/agents/`.

**What we build:** the engine, registry, event stream, API, UI, plugin, onboarding and readiness checks, run docs and history.

## Repo structure

The factory lives in one repo, `agent-factory`, with a separate folder per part. Backend and frontend share no code; the UI talks to the engine only through the API.

```
agent-factory/
  engine/       Python: engine, gates, registry, events, providers, API
    tests/
  cli/          Python: the factory command
  plugin/       Claude Code plugin: commands, skills, MCP server, hooks, status line
  ui/           React frontend, own package.json and tests
  templates/    intent.md, plan.md, handoff.md, review.md, report.md
  prompts/      default role prompts
  docs/
    design/design.md
    tasks/tasks.md
  CLAUDE.md
```

- Each part has its own test command, plus one top-level command that runs them all.
- **The HTTP API (FastAPI) arrives in milestone 5.** Until then the CLI calls the engine's Python code directly. From milestone 5, the plugin, terminal panes and UI all need to share one running engine, and the API is that shared door. It listens on localhost only.
- One repo keeps design, tasks and all parts in sync, and lets the factory later work on itself.

## Run flow

Every request follows the same flow; only the start differs. Each stage ends by writing a file the next stage reads, and a deterministic gate sits between stages.

&#91;embedded content: run flow · 10 steps, 2 approvals\]

A repo that isn't ready goes through bootstrap or onboarding first; failures loop back to the builder up to the cap, then stop and ask you.

**How the start differs**

| Request                         | What happens first                                                                                                                                                                                                                                 |
| ------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| New project                     | Intake creates the repo, then a bootstrap run sets up skeleton, test framework and smoke test. The planner then splits the project into features for you to approve; each feature runs the normal flow.                                            |
| New feature on an existing repo | Readiness check (onboarding once if never done). The planner reads the code and related past runs, asks questions only if something is unclear, and produces one plan or a split.                                                                  |
| A spec you provide              | The spec becomes `intent.md` unchanged. The planner first checks it: gaps, conflicts with existing code, open questions, size. Your answers are added. The reviewer later checks the diff against your spec, so nothing in it is silently dropped. |

**Gates between stages** (run by the engine, not by agents)

| After    | Check                                                                | On fail                                   |
| -------- | -------------------------------------------------------------------- | ----------------------------------------- |
| Planner  | `plan.md` names files, steps and measurable acceptance criteria      | Retry the planner with the reason         |
| Tester   | Tests exist and fail before the build                                | Retry the tester                          |
| Builder  | Engine runs the test command itself; diff touches only allowed paths | Resume the builder with the output, max 3 |
| Reviewer | No Important findings                                                | Resume the builder with `review.md`       |
| Any      | Cap reached                                                          | Stop and mark Needs you                   |

The tests written by the tester are locked from the builder by a hook, so the builder can't weaken them to pass.

## Per-repo configuration

Each repo carries its own factory configuration, so every repo can have different models, tests and safety rules. Settings are layered: a run-level override beats the repo's `factory.yaml`, which beats the global defaults.

**What lives in each repo**

- `CLAUDE.md`: commands, architecture, conventions, things agents get wrong.
- `factory.yaml`: models per role, test command, allowed paths per role, retry caps, provider, isolation.
- `.claude/agents/<role>.md`: that repo's prompt for planner, tester, builder, reviewer. The model always comes from `factory.yaml`.
- `docs/lessons.md` and `docs/runs/`: see History and docs.

**Settings per role**

| Setting         | Planner   | Tester       | Builder               | Reviewer    |
| --------------- | --------- | ------------ | --------------------- | ----------- |
| Default model   | Opus      | Sonnet       | Sonnet                | Opus        |
| Thinking effort | High      | Normal       | Normal                | High        |
| May write       | `plan.md` | `tests/**`   | `src/**`              | `review.md` |
| May not touch   | code      | `src/**`     | `tests/**`, `plan.md` | everything  |
| Shell commands  | read-only | test, lint   | test, build, lint     | none        |

Roles ask for logical models (`opus`, `sonnet`). Each provider profile maps those to real model names, so switching provider never means editing every repo.

**Provider profiles** (global default, overridable per repo and per run)

| Profile                  | Login                                          | Limit handling                                         | Usage visibility                       |
| ------------------------ | ---------------------------------------------- | ------------------------------------------------------ | -------------------------------------- |
| `subscription` (default) | Token from `claude setup-token`                | Shared plan limits; run pauses and resumes after reset | Tokens per run, recorded by the engine |
| `litellm`                | Proxy URL and a LiteLLM key, Bedrock behind it | Per-run budget on the key                              | Cost and tokens in proxy and engine    |

- The engine starts each agent with only its profile's credentials set. A proxy token outranks the subscription token in Claude Code, so a leftover variable would silently switch providers.
- A repo can list allowed providers. For example, a sensitive repo can allow only `litellm`.
- Before each run the engine checks the provider: token valid and not near expiry, or proxy reachable.

**Isolation setting**

- Global default by operating system: `worktree` on native Windows, `sandbox` on Linux and WSL.
- A repo can set `sandbox required`. The engine then refuses to run it on native Windows and says why.
- Each run's card and report show which isolation mode it ran under.

## Skills

Every stage agent is a full Claude Code session, so it uses skills like any session. The factory controls which skills each role gets, so runs stay focused and repeatable.

| Level        | Location                                                   | Examples                                                              |
| ------------ | ---------------------------------------------------------- | --------------------------------------------------------------------- |
| Factory-wide | The factory's own plugin                                   | `write-plan`, `write-handoff`, `review-rubric`, `acceptance-criteria` |
| Per repo     | `<repo>/.claude/skills/`, included in every run's worktree | API conventions, security rules, how to add a migration               |
| Personal     | Your `~/.claude/skills/`                                   | Not loaded by agents                                                  |

**Rules**

- `factory.yaml` lists the skills each role may use. No list means every role may use every repo skill.
- Each agent runs with its own factory config folder (`CLAUDE_CONFIG_DIR`), holding only the factory plugin and the auth token. Your personal skills, settings and history never leak into runs.
- A repo skill reaches runs only once committed to the base branch, since worktrees are created from it.
- A skill needs a `SKILL.md` with `name` and a `description` saying when to use it. The description decides whether the agent loads it.
- Skills advise; hooks enforce. A policy that must always hold also gets a hook or a gate.
- Skill changes go through a pull request, then the replay set runs to check quality didn't drop.
- Every loaded skill is logged as an event and shown in the swimlane. A review finding that keeps citing a policy a skill covers means the skill isn't triggering or has drifted.
- A mistake that repeats across runs becomes either a line in `lessons.md` (repo knowledge) or a skill (a repeatable procedure).

## Human decisions and feedback

You decide at four points; everything else runs on its own until a cap is hit. When a run needs you, it moves to Needs you on the board, the status line counts it, and a desktop notification fires.

**Your four decisions**

1. **Plan approval.** Check the acceptance criteria, because they become the tests. Approve, edit and approve, or send back with notes.
2. **Split approval.** When a task is too big for one merge request (about 400 changed lines or 10 files), the planner proposes parts with their order and dependencies. Approve, edit (merge, drop, reorder) or reject.
3. **Planner questions.** Gaps or conflicts in a request or spec. Your answers are added to `intent.md`.
4. **Merge.** The merge request arrives with passing tests and review findings attached.

**Splits stay visible.** The original task becomes a parent card showing parts done (for example 1 of 3). Each part is a normal run with its own merge request. A part that depends on another waits as blocked until that one merges, then starts on the updated code. The parent closes when every part is merged.

**Talking to an agent mid-run**

- Each agent is its own Claude Code session; the engine records its session ID in the run registry.
- **Agent running:** your message interrupts it; it continues in the same session with your note.
- **Agent finished:** the engine reopens its saved session, so it keeps what it already read and decided.
- **Take-over:** pause an agent and chat with its real session directly in a terminal pane, then hand it back.
- Feedback goes to the earliest stage it affects. Plan feedback reruns tester, builder and reviewer; test feedback reruns builder and reviewer; code feedback reruns tests and reviewer.
- After any chat-driven change, the engine reruns the tests and the review. Talking to an agent never skips a check.
- The run stays open until you close it, so you can fix things before closing. Every message and reply is recorded in the run's history.

## Git flow

Agents commit only on their run's branch; the engine does every push, rebase and merge. Agents use your own GitHub account; there is no bot account.

1. **Branch:** the engine creates `factory/run-<id>-<slug>` from the latest main, in a new worktree for that run.
2. **Commits:** agents commit on that branch only.
3. **Update before review:** the engine fetches main and rebases the run branch onto it. No conflicts: tests rerun. Conflicts: the builder resolves them, tests rerun, and if it still fails the run asks you.
4. **Push:** the engine pushes the run branch with a safe force-push, which refuses if the remote branch changed unexpectedly.
5. **Pull request:** opened from the run branch to main, with the plan, test output and review findings linked.
6. **Merge:** only after your approval, by you or by the engine on your approve action. If main moved again, the engine rebases and reruns tests first.
7. **Clean-up:** the worktree is removed after merge, or after 7 days if the run is abandoned.

**Limits enforced by the hook and engine**

- Agents may not run `git push`, `rebase`, `reset --hard` or branch deletes.
- Force-push only to the run's own branch, never to main or other branches.
- No code path merges into main without your approval.
- Optional: branch protection on main requiring a pull request, with yourself allowed to bypass. It catches an accidental direct push from a script bug.
- Two runs on the same repo that touch overlapping files are queued; runs on different files go in parallel.

## History and docs

Agents learn from short run records and lessons, not from raw transcripts. History has three layers, each with its own home.

| Layer       | Contents                                                       | Where                                                                 | Used for                                |
| ----------- | -------------------------------------------------------------- | --------------------------------------------------------------------- | --------------------------------------- |
| Lessons     | `CLAUDE.md`, `docs/lessons.md`                                 | In the repo, committed                                                | Read by every agent, every run          |
| Run records | `intent.md`, `plan.md`, `handoff.md`, `review.md`, `report.md` | In the repo, `docs/runs/run-<id>-<slug>/`, merged with the run's code | New agents look up related past runs    |
| Raw data    | Session transcripts, event logs, registry, test output         | Factory folder, outside the repo                                      | Audit, resuming recent sessions, the UI |

**Rules**

- One folder per run, written only by that run, so parallel runs never conflict.
- A run's docs merge in the same pull request as its code; if the pull request is rejected, its docs don't land either.
- No shared index file in the repo. The factory builds the run index by reading `docs/runs/`.
- `CLAUDE.md` and `lessons.md` change only through review: agents propose edits inside the pull request and you approve.
- When the same mistake or fix appears twice across runs, it becomes a line in `lessons.md`.
- Run docs stay short, a page each at most.
- Every agent ends its stage by writing `handoff.md`: files touched, decisions, rejected options, gaps.

**Pointing at history**

- The planner searches the repo's run index first and lists the past runs it used in `plan.md`.
- You can point at a run directly: "follow how run-38 handled CSV encoding". The engine attaches that run's handoff and report.
- Reopening an old agent's exact session is for recent runs only. For older ones the code has changed, so a fresh agent with the run's files is better.
- Retention: transcripts 30 days; run records forever, since they live in git.

## Interfaces

Every interface calls the same engine actions (start, status, feedback, approve, stop, attach, usage), so whatever you do in one shows in the others.

| Where you are                                          | How you use it                                              |
| ------------------------------------------------------ | ----------------------------------------------------------- |
| Any Claude Code session (any folder, VS Code, desktop) | Plugin: slash commands, or plain chat through its MCP tools |
| Windows Terminal                                       | One pane per active agent, opened on attach                 |
| Browser                                                | React UI: board, swimlanes, approvals, usage                |
| Phone                                                  | Remote Control on the session you use as orchestrator       |
| Scripts                                                | The `factory` CLI                                           |

**Claude Code plugin** (installed once at user level, so every session has it)

- Slash commands: `/factory:start`, `/factory:status`, `/factory:feedback`, `/factory:approve`, `/factory:attach`, `/factory:usage`, `/factory:stop`. Inside a repo folder, the repo is picked up from the folder.
- A local MCP server exposing the engine actions as tools, so plain requests work: "how's the repo-b export going?"
- A skill with the routing rules: which run, which agent, when to ask you. If two runs match, it asks.
- A status line: active runs, runs needing you, today's tokens.
- Notification hooks for needs you and failed.

**Terminal panes** (Windows Terminal; tmux on Linux or WSL)

- On run start, the engine can open a pane per active agent. The pane streams that agent's work and sends what you type to it as feedback.
- Header per pane: run, agent, model, live tokens, state.
- Each run gets its own tab. Closing a pane doesn't stop the agent; reattach any time.
- Settings: auto-open on start, maximum panes per tab, auto-close finished panes.

**React UI** (live, no refresh button; desktop first)

1. **Runs board:** cards grouped Needs you, Running, Failed, Done. Each card: run ID, repo, task, current agent, elapsed time, attempt count, a four-dot stage bar, tokens. Filters by repo and status; a New run button. Split tasks show as a parent card with child runs.
2. **Run detail:** header with status, time, provider, models per role, isolation, and Stop, Resume, Open pull request. A swimlane with one row per agent and time left to right; each step is a block (file reads, edits, test runs, gate results), retries shown as repeated blocks. Click a block for its details. A feedback box at the bottom with a Send to agent selector.
3. **Approval view:** the plan, split or review findings to approve, with Approve, Edit and approve, Send back with notes.

Status colours are the same everywhere: blue running, amber needs you, red failed, green done, grey not started.

**Token visibility**

- Per agent and per attempt in the swimlane, per run on cards, live in pane headers.
- `/factory:usage` and the CLI give totals by run, repo, role and day.
- On LiteLLM, cost is shown too. On the subscription, tokens plus how often runs hit the usage limit; exact plan limits aren't visible to the engine.

## Bootstrap, onboarding and readiness

No feature run starts on a repo until it meets the readiness bar, because tests are how the factory checks its own work. Without them every gate passes automatically.

**Readiness bar** (checked by the engine before every run)

- One command each for build, test and lint, all passing.
- Smoke tests exist and pass.
- `CLAUDE.md` and `factory.yaml` exist.
- Each critical flow has at least one test.

**New project: bootstrap run (run 0)**

1. The planner proposes stack, folder layout and test setup. You approve, or answer its questions about the stack.
2. The builder creates the skeleton: dependencies, config, a test framework, one passing smoke test (the app starts, or a health endpoint returns 200), browser tests with Playwright if there is a UI.
3. It writes `CLAUDE.md` and `factory.yaml`.
4. You approve the merge. The repo is now ready.

After that every feature run is test-first: acceptance criteria become failing tests, then the builder makes them pass. The tests come from what you want, not from existing code.

**Existing repo: onboarding run**

1. Analyzer finds the stack, entry points, existing tests and critical flows.
2. Setup makes build, test and lint each a single command, and writes `CLAUDE.md` and `factory.yaml`.
3. Test writer adds characterization tests for smoke and critical flows, plus areas you plan to change soon. Don't chase coverage.
4. Reviewer, then you. For each covered flow you mark: correct (keep), a bug (record as known issue), or unknown (ask the team). Characterization tests record current behavior, bugs included.

**Keeping repos ready**

- Coverage grows with every feature run, where the work actually happens.
- Readiness is re-checked weekly. A repo that falls below the bar drops to limited mode.
- **Limited mode:** the factory plans and opens a draft pull request; a human verifies. No autonomous runs on repos without tests.

## Safety

On native Windows the worktree separates the code but does not confine the agent, so six controls take the sandbox's place. Prompts are never the boundary; permissions, hooks and engine checks are.

1. **No skipped permissions.** Each role runs with an allowlist of commands (for example `pytest`, `npm test`, `git status`). Anything else is blocked.
2. **Deny rules on file tools:** no reading or editing outside the run's worktree; no access to `.ssh`, `.aws`, `.env` files or other repos.
3. **Role-aware hook on every tool call:** blocks paths outside the worktree, `cd` out of it, deletes on parent folders, `git push`, `curl` and `wget`, writes to protected files, and secrets in diffs. A block explains itself to the agent.
4. **Clean environment per agent:** only the credentials its provider profile needs.
5. **Engine check after every stage:** main checkout unchanged, nothing changed outside the worktree, diff limited to the role's allowed paths. A failure stops the run and flags you.
6. **Optional, stronger:** run agents as a separate Windows user with access only to the worktree folders.

**Remaining risk.** Controls 1 to 3 see the commands an agent runs, not what they do internally. A test script could still write outside the worktree; control 5 catches it only afterwards. Acceptable for your own repos. For sensitive repos use option 6, or WSL with `sandbox required`.

**Credentials**

- The subscription token lives only on your machine, as an environment variable for the engine; never committed or shared.
- Agents never see your GitHub credentials; the engine does all git network operations.
- Test secrets for a repo live in a `.env.factory` file outside the repo, never production secrets.

**Terms of use for the subscription**

- Headless use with a `setup-token` token is documented for scripts and CI. Pro and Max limits assume ordinary individual use, so parallel agents hit limits faster.
- Keep it personal: only you use it. If it becomes a team tool, switch to API keys or Bedrock.
- Pro and Max fall under Anthropic's Consumer Terms; check them before using the subscription on code you don't own.

## Failure handling, limits and quality

A run never fails silently: it either recovers by itself within its caps or stops and asks you.

**Failures**

| Situation                                  | What the engine does                                                |
| ------------------------------------------ | ------------------------------------------------------------------- |
| Tests fail after build                     | Resume the builder with the output, up to 3 attempts                |
| Review fails                               | Resume the builder with `review.md`, within the same cap            |
| Plan invalid (no files, steps or criteria) | Retry the planner with the reason                                   |
| Tests don't fail before the build          | Retry the tester                                                    |
| Cap reached                                | Mark Needs you and stop                                             |
| Usage limit hit (subscription)             | Pause, record where it stopped, resume the same session after reset |
| Budget hit (LiteLLM)                       | Pause and ask: raise budget or stop                                 |
| No tool activity for 10 minutes            | Stop the agent and flag the run                                     |
| Engine crash or machine restart            | Continue from the last completed step in `run.json`                 |
| Turn or time cap per run                   | Pause and ask                                                       |

**Concurrency:** 2 agents at once to start, since all share one subscription. Extra runs wait in a queue and start as slots free up.

**Quality metrics** (tracked from day one, per run and over time)

- First-pass success: the share of runs whose first build passes tests and review.
- Builder retries per run.
- Important review findings per run.
- How much you change the pull request before merging.
- Tokens per run and per role.

**Replay set:** keep 10 to 15 finished tasks with their accepted outcomes. Rerun them whenever prompts, models or configuration change, and compare the metrics. Every production bug that slips through becomes a new replay case.

## Build order and setup

Build the engine and its checks first and the UI last: the gates decide whether output is good, the UI only shows it. Each milestone finishes only when its exit check passes.

1. **Prompts and templates by hand.** Write the templates (`intent.md`, `plan.md`, `handoff.md`, `review.md`, `report.md`) and the role prompts, then run one real task manually. Exit: the manual result is good enough to merge.
2. **Engine and CLI, one repo.** State machine, gates, registry, events, provider profiles, the safety hook, the bootstrap run on your new project. Exit: bootstrap plus 3 feature runs completed end to end from the CLI.
3. **Run docs, history and split handling.** Exit: a split task finishes as a parent with merged parts, docs in `docs/runs/`.
4. **Multiple repos and the queue.** Exit: two repos running in parallel without interference.
5. HTTP API, **Claude Code plugin and terminal panes.** Exit: start, steer and approve a run entirely from a Claude Code session.
6. **React UI.** Board, swimlane, approvals, usage. Start only after milestones 2 to 4 have run reliably for one to two weeks.
7. **Triggers.** GitHub or Jira issues start runs automatically.

After milestone 2, give the factory its own repo as a project; building itself surfaces problems fastest.

**Setup before development**

- [ ] Install Windows Terminal, Git and Python; confirm Claude Code is installed and logged in on your Max plan
- [ ] Run `claude setup-token` once and store the token as an environment variable on your machine
- [ ] Create a private repo for the factory itself
- [ ] Create the repo for your new project on GitHub
- [ ] Optional: protect main on the new project's repo, allowing yourself to bypass

**Open items, none blocking**

- The new project's language and framework. The bootstrap planner will ask if not decided.
- Max plan tier (5x or 20x), which sets how many agents can run in parallel.
