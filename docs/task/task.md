# Software Factory — Tasks

Source of truth: [docs/design/design.md](../design/design.md). This file breaks it into the 7 milestones from "Build order and setup".

**How to use this file**

- Take the first unchecked task whose dependencies are all checked.
- Each task is one commit. Tests first, then code; `uv run tools/dev.py test` and `uv run tools/dev.py lint` must pass before ticking it.
- Each milestone ends with a **manual exit task**. Its checklist is signed off by Kunal, not by an agent.
- Milestones 1 and 2 are written at full task detail. Milestones 3–7 list features and exit checks only; each is expanded into tasks at the start of that milestone, and the expansion is reviewed before work starts.

**Decisions already made (from design review, Oct 9, 2026)**

| Topic | Decision |
| --- | --- |
| Onboarding of existing repos | Milestone 4 |
| Readiness check before each run | Milestone 2 |
| Weekly readiness re-check, limited mode | Milestone 4 |
| Quality metrics | Milestone 2 records the data per run; reporting arrives with the UI (milestone 6) |
| Replay set | Milestone 3 |
| Usage-limit pause/resume, 10-minute inactivity stop, crash recovery | Milestone 2 |
| Mid-run feedback, interrupt, resume from the CLI | Milestone 2; terminal-pane take-over in milestone 5 |
| Desktop notifications | Milestone 5 |
| Providers | Milestone 2: `subscription` only, plus the profile layer and credential isolation. Full `litellm` (budget, cost) in milestone 4 |
| `sandbox` isolation | Detect and refuse with a clear message. Sandbox mode is after version 1 |
| Human decisions | Four. Split approval and planner questions happen inside the planner step |
| Handoff files | Every role may write its own handoff file in the run's folder, `handoff-<role>.md`, all from one template. The reviewer writes `review.md` only. Each role reads the handoffs of all earlier roles first. `handoff-builder.md` holds the latest attempt; the engine archives earlier ones to `attempts/handoff-builder-<n>.md` (agreed Oct 10, 2026) |
| Commits | Agents never run `git`. The engine commits once after each passing stage gate, with a message like `factory(run-42): builder attempt 2`. The shell allowlists stay exactly as in the design's role table. Deviation from the design's "agents commit on that branch", approved by Kunal (Oct 10, 2026) |
| Bootstrap write paths | The bootstrap builder writes the whole worktree except `plan.md`, through a bootstrap override in the global defaults, not hard-coded. Details settled at the start of 2.1 |
| Run docs before milestone 3 | Live in the factory data folder, outside the repo |
| Factory-wide skills | Written in `plugin/skills/` in milestone 1; the engine loads them into each agent's config folder from milestone 2. The rest of the plugin waits for milestone 5 |
| Triggers | GitHub issues with label `factory`, by polling. Jira later |
| Overlapping runs | If two plans list overlapping files, queue the second run; verify afterwards with the actual diff |
| HTTP API | Milestone 5 only. Until then the CLI calls engine code directly |

---

## Setup before development

Manual, done by Kunal. Not commits.

- [x] S.1 Install Windows Terminal, Git and Python 3.12; confirm Claude Code is installed and logged in on the Max plan
  - Max plan tier: 5x.
- [x] S.2 Install `uv` and the GitHub CLI (`gh`), then run `gh auth login`. (Task 2.14 needs `gh`.)
  - `gh` 2.102.0 installed on Oct 9, 2026.
- [x] S.3 Run `claude setup-token` once and store the token as the user environment variable `CLAUDE_CODE_OAUTH_TOKEN`; note its expiry date in the global settings (task 2.6)
  - Token expires 2026-11-10. Renew it before then and record the date in the global settings (task 2.6).
- [x] S.4 Create a private GitHub repo for the factory itself and push this repo to it
  - Deviation: `kmugalkhod/agent-factory` is public, by Kunal's choice.
- [ ] S.5 Create the GitHub repo for the new pilot project (empty, with a README)
  - Deferred: the pilot project isn't chosen yet. It is first needed for the milestone 2 exit (bootstrap run).
- [ ] S.6 Optional: protect `main` on the pilot repo, allowing yourself to bypass
  - Skipped by Kunal's choice.

---

## Milestone 1 — Prompts and templates by hand

Goal: the files every stage writes and reads exist, the role prompts and factory skills exist, and one real task done by hand with them is good enough to merge.

### [x] 1.1 Repo skeleton and dev tool

- **What:** Create the uv workspace with the `engine` and `cli` packages (empty modules, one trivial test each), shared ruff/pyright/pytest config, and `tools/dev.py` with `setup`, `test` and `lint`, each taking an optional part (`engine`, `cli`, `plugin`, `ui`, `all`). Parts that don't exist yet are skipped with a message.
- **Touches:** `pyproject.toml` (workspace root), `engine/`, `engine/tests/`, `cli/`, `tools/dev.py`, `tools/tests/`, `.gitignore`, `.python-version`
- **Acceptance criteria:**
  - On a clean clone, `uv run tools/dev.py setup`, `test` and `lint` each exit 0.
  - `uv run tools/dev.py test engine` runs only the engine tests.
  - A failing test or lint error in any part makes the matching command exit non-zero.
  - `lint` runs `ruff check`, `ruff format --check` and `pyright` (basic mode).
- **Tests:** `tools/tests/test_dev.py` checks part selection, the skip message for missing parts, and that a non-zero exit is propagated (using a stub runner).
- **Depends on:** S.1, S.2

### [x] 1.2 Run document templates

- **What:** Write `intent.md`, `plan.md`, `handoff.md`, `review.md` and `report.md`, each at most one page. Fix the exact headings the engine gates will parse later, and document them in `templates/README.md`.
- **Touches:** `templates/`, `tools/tests/`
- **Acceptance criteria:**
  - `plan.md` has these sections: Context, Past runs used, Files, Steps, Acceptance criteria (numbered and measurable), Test cases, Split (optional), Questions (optional).
  - `review.md` has a findings list with severity `Important` / `Minor` / `Nit` and a single verdict line.
  - `handoff.md` has: Files touched, Decisions, Rejected options, Gaps.
  - `templates/README.md` lists every required heading for each template.
- **Tests:** `tools/tests/test_templates.py` checks that each template contains exactly the required headings listed in `templates/README.md`.
- **Depends on:** 1.1

### [x] 1.3 Default role prompts

- **What:** Write the default prompts for planner, tester, builder and reviewer, plus a bootstrap addendum for the planner and builder. Each prompt states the role's output file, allowed write paths, shell limits and the handoff rule, matching the role table in the design.
- **Touches:** `prompts/planner.md`, `prompts/tester.md`, `prompts/builder.md`, `prompts/reviewer.md`, `prompts/bootstrap.md`, `tools/tests/`
- **Acceptance criteria:**
  - Each prompt names its template file and its allowed write paths exactly as in the design's "Settings per role" table.
  - The tester prompt requires tests that fail before the build.
  - The builder prompt forbids editing tests.
  - The reviewer prompt checks the diff against `intent.md` and `plan.md`.
  - The planner prompt says to ask questions only when something is unclear, to propose a split above about 400 lines or 10 files, and to list the past runs it used.
- **Tests:** `tools/tests/test_prompts.py` checks that each prompt mentions its output file, `handoff`, and its allowed paths.
- **Depends on:** 1.2

### [ ] 1.4 Factory-wide skills

- **What:** Write the factory skills the agents use: `write-plan`, `write-handoff`, `review-rubric` and `acceptance-criteria`. Each skill points at its template and gives a short procedure with examples.
- **Touches:** `plugin/skills/<name>/SKILL.md`, `tools/tests/`
- **Acceptance criteria:**
  - Each `SKILL.md` has YAML frontmatter with `name` and a `description` that says when to use the skill.
  - `name` matches the folder name.
  - Each body is under 150 lines.
- **Tests:** `tools/tests/test_skills.py` validates the frontmatter, the name/folder match and the line limit for every folder in `plugin/skills/`.
- **Depends on:** 1.2

### [ ] 1.5 Manual run on this repo

- **What:** Do task 2.1 by hand with the milestone 1 files. Run a separate Claude Code session per role (planner, then tester, builder and reviewer), each given only its role prompt and the previous stage's files. Keep the run docs in a scratch folder outside the repo. Fix the prompts, templates and skills wherever they fell short.
- **Touches:** `prompts/`, `templates/`, `plugin/skills/` (fixes only); the task 2.1 code is committed under task 2.1
- **Acceptance criteria:**
  - All five run docs were produced.
  - The tester's tests failed before the build and pass after it.
  - The review has no Important findings, or they were fixed by resuming the builder.
  - Every change made to a prompt, template or skill is listed in the task 1.5 commit message.
- **Tests:** `uv run tools/dev.py test` and `lint` pass after the fixes.
- **Depends on:** 1.3, 1.4

### [ ] 1.X Milestone 1 exit (manual, signed off by Kunal)

- [ ] The manual result of task 2.1 is good enough to merge, and is merged
- [ ] The run docs from 1.5 are each at most one page and readable without the transcript
- [ ] Prompt and template fixes from 1.5 are committed
- [ ] Milestone summary written: what was built, what was tested, deviations from the design

---

## Milestone 2 — Engine and CLI, one repo

Goal: the deterministic engine runs bootstrap and feature runs end to end on one repo from the CLI, with gates, the safety hook, the registry, events and the subscription provider. One run at a time. No HTTP API.

All engine code lives in `engine/factory_engine/`; tests in `engine/tests/`. Tests use a fake agent runner and temporary git repos. Tests that call a real model are marked `@pytest.mark.live` and are skipped by default.

### [ ] 2.1 `factory.yaml` schema and layered settings

- **What:** Add pydantic models for the global defaults, the repo's `factory.yaml` and run-level overrides, merged in the order run > repo > global. Fields:
  - models and thinking effort per role
  - test, build, lint and smoke commands
  - allowed write paths, shell allowlist and skills per role
  - retry caps, turn and time caps
  - provider, allowed providers
  - isolation and `sandbox_required`
  - critical flows

  Include the bootstrap override of the builder's write paths in the global defaults (see the decisions table); settle its shape when this task starts.
- **Touches:** `engine/factory_engine/config.py`, `engine/tests/test_config.py`
- **Acceptance criteria:**
  - Defaults match the design's role table: models, effort, allowed paths, shell access.
  - The precedence run > repo > global holds for every field.
  - An unknown key or a wrong type fails with the file and key path in the message.
  - Logical model names (`opus`, `sonnet`) are kept unresolved at this layer.
- **Tests:** Unit tests for precedence, defaults, validation errors, and a full sample `factory.yaml` round trip.
- **Depends on:** 1.1 (done by hand in 1.5)

### [ ] 2.2 Data folder and `run.json`

- **What:** Add the factory data folder (`%LOCALAPPDATA%\agent-factory`, overridable by an env var) and the `Run` model saved as `run.json`:
  - id, slug, repo, kind (bootstrap / feature / spec), state, stage, attempts
  - session ID per role, provider, isolation, timestamps, metrics
  - last completed step

  Writes are atomic (temp file plus replace).
- **Touches:** `engine/factory_engine/paths.py`, `engine/factory_engine/run.py`, `engine/tests/`
- **Acceptance criteria:**
  - Save then load gives an equal object.
  - A write interrupted before replace leaves the previous `run.json` valid.
  - Each run's docs folder is `<data>/runs/<id>-<slug>/`.
  - Run IDs are unique and increase per repo.
- **Tests:** Round-trip test, simulated interrupted write, ID allocation test.
- **Depends on:** 2.1

### [ ] 2.3 Registry in SQLite

- **What:** Add a SQLite registry for repos, runs (an index over `run.json`) and agent sessions, with schema versioning via `PRAGMA user_version`, WAL mode, and functions to rebuild the index from the run folders.
- **Touches:** `engine/factory_engine/registry.py`, `engine/tests/test_registry.py`
- **Acceptance criteria:**
  - Create, list and update work for repos, runs and sessions.
  - A newer schema version on disk is refused with a clear error.
  - Rebuilding from run folders reproduces the index exactly.
  - Two connections can read while one writes.
- **Tests:** Unit tests on a temporary database, including rebuild and migration from version 0.
- **Depends on:** 2.2

### [ ] 2.4 Event stream

- **What:** Add an append-only `events.jsonl` per run with typed events:
  - `state_changed`, `stage_started`, `stage_finished`
  - `tool_call`, `skill_loaded`, `gate_result`, `test_run`
  - `tokens`, `message`, `needs_you`

  Also a reader that tails from a sequence number.
- **Touches:** `engine/factory_engine/events.py`, `engine/tests/test_events.py`
- **Acceptance criteria:**
  - Sequence numbers are strictly increasing per run.
  - A partly written last line is ignored on read and does not break later appends.
  - The reader returns only events after a given sequence number.
  - Every event has a timestamp, run ID and role (if any).
- **Tests:** Unit tests for append, tail, truncated-line recovery and schema validation.
- **Depends on:** 2.2

### [ ] 2.5 Run state machine

- **What:** Add a pure transition function over run states:
  - pending → planning → awaiting_plan_approval → testing → building → reviewing → updating → awaiting_merge → merged
  - plus needs_you, paused, failed, stopped and closed

  Illegal transitions raise. Every transition emits `state_changed`.
- **Touches:** `engine/factory_engine/states.py`, `engine/tests/test_states.py`
- **Acceptance criteria:** A table test covers every legal transition and asserts that every other pair raises.
- **Tests:** Exhaustive table test, plus an event-emission test.
- **Depends on:** 2.4

### [ ] 2.6 Provider profiles and credential isolation

- **What:** Add the provider profile layer (logical model → real model name) and the `subscription` profile. Build each agent's environment from a clean base:
  - Remove all `ANTHROPIC_*`, proxy and Claude credential variables.
  - Set only the profile's credentials and `CLAUDE_CONFIG_DIR`.

  Run a preflight before each run: token present, and not within 14 days of the expiry date recorded in global settings. `litellm` and unknown profiles are refused with "available from milestone 4". The repo's allowed-providers list is enforced.
- **Touches:** `engine/factory_engine/providers.py`, `engine/tests/test_providers.py`
- **Acceptance criteria:**
  - With `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN` set in the parent environment, the agent environment contains none of them.
  - Logical models resolve per profile.
  - Preflight fails with a specific message for a missing token, a near-expiry token and a disallowed provider.
- **Tests:** Unit tests with a fake parent environment.
- **Depends on:** 2.1

### [ ] 2.7 Per-agent config folder

- **What:** For each run and role, create a `CLAUDE_CONFIG_DIR` holding:
  - a `settings.json` with the role's allow and deny rules
  - only the factory skills allowed by `factory.yaml` (copied from `plugin/skills/`)

  No personal settings, skills or history.
- **Touches:** `engine/factory_engine/agent_config.py`, `engine/tests/test_agent_config.py`
- **Acceptance criteria:**
  - Deny rules cover reads and edits outside the worktree, `.ssh`, `.aws`, `.env*` and other repos.
  - The allow rules equal the role's shell allowlist.
  - The skills present equal the role's skill list (all skills if no list is given).
  - Nothing from `~/.claude` is copied.
- **Tests:** Unit tests inspect the generated folder for each role.
- **Depends on:** 1.4, 2.1, 2.6

### [ ] 2.8 Safety hook: file paths

- **What:** Add a role-aware PreToolUse check for file tools (Read, Write, Edit, Glob, Grep, NotebookEdit). It blocks:
  - paths outside the worktree
  - protected files
  - writes outside the role's allowed paths
  - writes by the builder to files the tester created (test lock)

  Each block returns a reason for the agent.
- **Touches:** `engine/factory_engine/safety/paths.py`, `engine/tests/safety/`
- **Acceptance criteria:** A table of at least 40 cases passes, including:
  - `..` traversal, other drive letters, UNC paths
  - mixed slashes, case differences
  - junctions and symlinks resolving outside the worktree
  - 8.3 short names
  - the test lock

  Every block has a non-empty reason.
- **Tests:** Table-driven unit tests, plus junction cases on a temporary folder.
- **Depends on:** 2.1

### [ ] 2.9 Safety hook: shell commands

- **What:** Add a PreToolUse check for Bash and PowerShell. It splits compound commands (`&&`, `||`, `;`, `|`, subshells) and checks each part against the role's allowlist. It also blocks:
  - every `git` command, for every role (agents never run git; the engine commits)
  - `curl`, `wget`, `Invoke-WebRequest` and `iwr`
  - `cd` out of the worktree
  - deletes on parent folders
- **Touches:** `engine/factory_engine/safety/commands.py`, `engine/tests/safety/`
- **Acceptance criteria:**
  - A table of at least 50 cases passes, including obfuscation attempts (quoting, `cmd /c`, `powershell -c`, environment variable expansion).
  - The reviewer is denied every shell command.
  - Every role is denied every `git` command, including read-only ones such as `git log`.
  - The planner is allowed only read-only commands.
- **Tests:** Table-driven unit tests.
- **Depends on:** 2.1

### [ ] 2.10 Secret scan

- **What:** Add a scanner for secrets in text and diffs: Anthropic, AWS and GitHub tokens, private keys, and assignments copied from `.env` files. It is used by the hook on Write and Edit content and by the post-stage check.
- **Touches:** `engine/factory_engine/safety/secrets.py`, `engine/tests/safety/`
- **Acceptance criteria:**
  - Every fixture secret is detected.
  - There are no hits on a set of clean fixtures (lock files, base64 test data, UUIDs).
  - Findings never include the full secret value.
- **Tests:** Fixture-based unit tests.
- **Depends on:** 1.1

### [ ] 2.11 Hook wiring

- **What:** Combine 2.8–2.10 into one PreToolUse hook callback for the Agent SDK. It logs every decision as a `tool_call` event (allowed or blocked, with the reason) and logs `skill_loaded` events.
- **Touches:** `engine/factory_engine/safety/hook.py`, `engine/tests/safety/`
- **Acceptance criteria:**
  - A blocked call returns a deny decision with the reason.
  - Allowed calls pass through.
  - Every decision appears in `events.jsonl`.
  - An exception inside the hook denies the call (fail closed).
- **Tests:** Unit tests calling the callback with SDK-shaped `PreToolUseHookInput` dicts.
- **Depends on:** 2.4, 2.8, 2.9, 2.10

### [ ] 2.12 Agent runner

- **What:** Add an `AgentRunner` protocol and its Claude Agent SDK implementation. It builds the agent options:
  - cwd = worktree
  - system prompt = `prompts/<role>.md` plus the repo's `.claude/agents/<role>.md`
  - model from the profile, thinking effort
  - env from 2.6, hooks from 2.11, allowed tools
  - `resume`

  It streams messages to events, stores the session ID and sums tokens per attempt. It also supports `interrupt` and `send`. A `FakeAgentRunner` replays scripted messages for tests.
- **Touches:** `engine/factory_engine/agents/runner.py`, `engine/factory_engine/agents/fake.py`, `engine/tests/agents/`
- **Acceptance criteria:**
  - With the fake runner, the session ID is saved to `run.json` and the registry.
  - Tokens per role and attempt equal the scripted totals.
  - `resume` passes the saved session ID.
  - A live test (marked `live`) completes a one-line task in a temporary repo.
- **Tests:** Unit tests with the fake runner and a fake SDK transport; one live test.
- **Depends on:** 2.3, 2.6, 2.7, 2.11

### [ ] 2.13 Watchdog and usage-limit pause

- **What:** Add two behaviors to the runner:
  - If there is no tool activity for 10 minutes (configurable), stop the agent and flag the run as Needs you.
  - If the subscription usage limit is hit, pause the run, record where it stopped and the reset time, and resume the same session after the reset.

  Turn and time caps per run pause the run and ask.
- **Touches:** `engine/factory_engine/agents/watchdog.py`, `engine/factory_engine/agents/runner.py`, `engine/tests/agents/`
- **Acceptance criteria:**
  - Using a fake clock, a stall of 10 minutes stops the agent and moves the run to needs_you.
  - A scripted usage-limit message moves the run to paused with a resume time.
  - After that time, the same session is resumed.
  - Hitting a cap pauses the run with the reason.
- **Tests:** Unit tests with a fake clock and the fake runner.
- **Depends on:** 2.5, 2.12

### [ ] 2.14 Git and GitHub operations

- **What:** Add git and GitHub operations through `subprocess`:
  - Create branch `factory/run-<id>-<slug>` from the latest `main` in a new worktree under the data folder.
  - Commit the stage's changes in the worktree, with the message `factory(run-<id>): <role> attempt <n>`.
  - Fetch and rebase onto `main`.
  - Push with `--force-with-lease`, to the run branch only.
  - Open a PR with `gh`, with the plan, test output and findings in the body. Merge with `gh`.
  - Remove the worktree.
  - Clean up abandoned worktrees after 7 days.
- **Touches:** `engine/factory_engine/git_ops.py`, `engine/factory_engine/github.py`, `engine/tests/`
- **Acceptance criteria:**
  - Against a temporary bare "remote", the branch, rebase, push and lease refusal behave as the design describes.
  - Pushing any branch other than the run branch raises.
  - A stage commit has the message `factory(run-<id>): <role> attempt <n>` and is made on the run branch only.
  - `gh` calls are checked with a fake `gh` executable on `PATH`.
  - No call uses `shell=True`.
- **Tests:** Integration tests with temporary git repos and a fake `gh`.
- **Depends on:** 2.2, S.2

### [ ] 2.15 Post-stage engine check

- **What:** After every stage, check that:
  - the main checkout is unchanged (HEAD, index and working tree)
  - nothing outside the worktree changed in the watched folders
  - the stage's diff touches only the role's allowed paths
  - the secret scan of the diff is clean

  Any failure stops the run and flags it.
- **Touches:** `engine/factory_engine/gates/post_stage.py`, `engine/tests/gates/`
- **Acceptance criteria:** Each of the four failure kinds, simulated in a temporary repo, stops the run with a specific `gate_result` event. A clean stage passes.
- **Tests:** Integration tests with temporary repos.
- **Depends on:** 2.8, 2.10, 2.14

### [ ] 2.16 Planner gate

- **What:** Parse `plan.md` using the headings fixed in 1.2. Pass only when it has:
  - files and steps
  - at least one numbered acceptance criterion that is measurable (names a command, test, value or observable behaviour)

  Return the Split and Questions sections as structured data. On fail, give the reason to the planner retry.
- **Touches:** `engine/factory_engine/gates/plan.py`, `engine/tests/gates/`
- **Acceptance criteria:**
  - A good plan passes.
  - Missing files, missing steps, no criteria and vague-only criteria each fail with a distinct reason.
  - Split and Questions sections are returned when present.
- **Tests:** Fixture `plan.md` files, good and bad.
- **Depends on:** 1.2

### [ ] 2.17 Tester gate

- **What:** Check that the tester's diff adds test files under its allowed paths. The engine runs the repo's test command itself and requires it to fail. For pytest and vitest it reads the JUnit XML report and requires that the new tests fail and that there are no collection or syntax errors. On fail, retry the tester with the reason.
- **Touches:** `engine/factory_engine/gates/tests_gate.py`, `engine/factory_engine/testrun.py`, `engine/tests/gates/`
- **Acceptance criteria:**
  - No new tests → fail.
  - Tests that pass before the build → fail.
  - A collection or syntax error → fail with "broken tests", not "tests fail".
  - Correctly failing new tests → pass.
  - Test output is saved to the run folder.
- **Tests:** Integration tests running pytest on small fixture repos.
- **Depends on:** 2.14, 2.15

### [ ] 2.18 Builder gate and test lock

- **What:** Run the repo's test command (with a timeout) and record its output. Check that the diff is within the builder's allowed paths. Check that every file the tester created is byte-identical to the tester's version (hash check). On fail, resume the builder with the output, within the cap.
- **Touches:** `engine/factory_engine/gates/build.py`, `engine/tests/gates/`
- **Acceptance criteria:**
  - Passing tests and a clean diff → pass.
  - Failing tests → fail with the output attached.
  - A changed or deleted test file → fail and flag, even if the hook missed it.
  - A test command timeout → fail with "timeout".
- **Tests:** Integration tests on fixture repos.
- **Depends on:** 2.17

### [ ] 2.19 Reviewer gate

- **What:** Parse `review.md` findings and the verdict. Any Important finding → fail, and resume the builder with `review.md`. The reviewer writes `review.md` only, which serves as its handoff; its stage must leave the worktree unchanged.
- **Touches:** `engine/factory_engine/gates/review.py`, `engine/tests/gates/`
- **Acceptance criteria:**
  - No Important findings → pass.
  - One or more → fail, and the count is recorded.
  - A malformed `review.md` → fail with a retry of the reviewer, not of the builder.
- **Tests:** Fixture `review.md` files.
- **Depends on:** 1.2

### [ ] 2.20 Stage pipeline with retries and caps

- **What:** Add the orchestration loop that runs each stage, its gate, the post-stage check and the transition. Retry rules:
  - planner retry with the reason
  - tester retry
  - builder resume with the output
  - review failures resume the builder within the same cap of 3

  After each passing stage gate, commit the stage's changes (2.14). Before each builder retry, copy `handoff-builder.md` to `attempts/handoff-builder-<n>.md` in the run folder. When a cap is reached, mark the run Needs you and stop. Save the last completed step after every step.
- **Touches:** `engine/factory_engine/pipeline.py`, `engine/tests/test_pipeline.py`
- **Acceptance criteria:** Scripted fake-runner scenarios produce the expected state and event sequence:
  - happy path
  - bad plan then a good one
  - passing tests then failing ones
  - builder failing three times → needs_you, with `attempts/handoff-builder-1.md` and `-2.md` kept and `handoff-builder.md` holding attempt 3
  - review with an Important finding → builder → pass
- **Tests:** Scenario tests with the fake runner and fixture repos.
- **Depends on:** 2.5, 2.13, 2.15, 2.16, 2.17, 2.18, 2.19

### [ ] 2.21 Intake, plan approval and planner questions

- **What:** Start a run from a feature description or a spec file. A spec is copied into `intent.md` unchanged. Pause for the four decisions:
  - Questions: your answers are appended to `intent.md` and the planner reruns.
  - Plan approval: approve, edit-and-approve, or send back with notes.
  - Split approval: in this milestone a split marks the run Needs you with "splits arrive in milestone 3".
  - Merge approval: handled in task 2.22.
- **Touches:** `engine/factory_engine/intake.py`, `engine/factory_engine/decisions.py`, `engine/tests/`
- **Acceptance criteria:**
  - A spec's bytes are preserved in `intent.md`.
  - Answers are appended under a dated heading.
  - Each decision action produces the right transition and event.
  - Send-back passes the notes to the planner.
- **Tests:** Unit and scenario tests with the fake runner.
- **Depends on:** 2.20

### [ ] 2.22 Update, push, pull request and merge

- **What:** Implement git flow steps 3–7:
  - Rebase onto `main` before review. No conflicts → tests rerun. Conflicts → the builder resolves them, tests rerun, and if they still fail the run asks you.
  - Push and open the PR.
  - Merge only on your approve action; if `main` moved again, rebase and rerun tests first.
  - Clean up the worktree afterwards.
- **Touches:** `engine/factory_engine/pipeline.py`, `engine/factory_engine/merge.py`, `engine/tests/`
- **Acceptance criteria:**
  - No code path merges without an approve action (asserted by a test that runs every path).
  - `main` moving between approval and merge triggers a rebase and a rerun.
  - An unresolved conflict → needs_you.
  - The worktree is removed after merge.
- **Tests:** Integration tests with temporary repos, a bare remote and a fake `gh`.
- **Depends on:** 2.14, 2.20, 2.21

### [ ] 2.23 Feedback, interrupt and resume

- **What:** Add `feedback(run, role, message)`:
  - If the agent is running, interrupt it and continue the same session with the note.
  - If it has finished, resume its saved session.

  Rerun from the earliest affected stage: plan → tester, builder, reviewer; tests → builder, reviewer; code → tests, reviewer. Tests and review always rerun after a chat-driven change. Every message is recorded.
- **Touches:** `engine/factory_engine/feedback.py`, `engine/tests/`
- **Acceptance criteria:**
  - Each of the three feedback targets reruns exactly the stages listed.
  - A message to a running agent calls `interrupt` and then `send` on the same session.
  - Messages and replies appear as `message` events.
- **Tests:** Scenario tests with the fake runner.
- **Depends on:** 2.20

### [ ] 2.24 Crash recovery

- **What:** On engine start, find runs that aren't finished and continue each one from the last completed step in its `run.json`. A run that was mid-stage resumes that agent's session.
- **Touches:** `engine/factory_engine/recovery.py`, `engine/tests/`
- **Acceptance criteria:**
  - A run killed at each step of the pipeline (simulated) resumes and finishes with the same end state as an uninterrupted run.
  - No stage that has already passed reruns.
- **Tests:** Parametrized kill-and-resume tests with the fake runner.
- **Depends on:** 2.20

### [ ] 2.25 Readiness check

- **What:** Before every feature run, check that:
  - the build, test and lint commands exist and pass
  - the smoke test exists and passes
  - `CLAUDE.md` and `factory.yaml` exist
  - every critical flow in `factory.yaml` maps to at least one existing test

  A repo with `sandbox_required` on native Windows is refused, with the reason given.
- **Touches:** `engine/factory_engine/readiness.py`, `engine/tests/`
- **Acceptance criteria:**
  - Each failing condition gives its own message, and the run doesn't start.
  - A ready fixture repo passes.
  - The `sandbox_required` refusal names the alternatives (WSL, after version 1).
- **Tests:** Fixture repos, one per failing condition.
- **Depends on:** 2.1

### [ ] 2.26 Bootstrap run

- **What:** Add the new-project flow (run 0) on an existing, empty GitHub repo:
  - The planner proposes stack, layout and test setup, and asks you when the stack isn't decided.
  - The builder creates the skeleton, a smoke test, `CLAUDE.md` and `factory.yaml`, plus Playwright if there's a UI.
  - The engine runs the readiness check before you approve the merge.
- **Touches:** `engine/factory_engine/bootstrap.py`, `prompts/bootstrap.md`, `engine/tests/`
- **Acceptance criteria:**
  - With the fake runner, the run reaches awaiting_merge only if the readiness check passes on the run branch.
  - With no stack decided, the planner's questions pause the run.
  - After merge, the repo is marked ready in the registry.
- **Tests:** Scenario tests with the fake runner and an empty fixture repo; one live test (marked `live`).
- **Depends on:** 2.21, 2.22, 2.25

### [ ] 2.27 Run metrics and report

- **What:** Record per run:
  - first-pass result (first build passed tests and review)
  - builder retries
  - Important findings
  - tokens per role and per attempt
  - lines you changed on the PR before merge

  Write `report.md` at the end of the run into the run's folder.
- **Touches:** `engine/factory_engine/metrics.py`, `engine/factory_engine/report.py`, `engine/tests/`
- **Acceptance criteria:**
  - Scripted scenarios produce the expected metric values in `run.json` and the registry.
  - `report.md` follows the template and is at most one page.
- **Tests:** Unit and scenario tests.
- **Depends on:** 2.20, 2.22

### [ ] 2.28 CLI: run commands

- **What:** Add the `factory` command (Typer) with these subcommands:
  - `start` (`--feature`, `--spec`, `--new-project`; the repo is taken from the current folder unless given)
  - `status`, `approve`, `feedback`, `stop`, `resume`

  The CLI calls engine code directly and runs the pipeline in the foreground until the run needs you or finishes. `attach` prints the command to resume the agent's session in a terminal.
- **Touches:** `cli/`, `cli/tests/`
- **Acceptance criteria:**
  - Each command calls the matching engine action. Checked with an engine stub.
  - Exit codes: 0 done, 2 needs you, 1 error.
  - `status` lists runs with state, stage, attempts and tokens.
- **Tests:** Typer `CliRunner` tests against an engine stub.
- **Depends on:** 2.21, 2.23, 2.24, 2.26

### [ ] 2.29 CLI: usage

- **What:** Add `factory usage` with token totals grouped by run, repo, role and day, read from the registry, plus how often runs hit the usage limit.
- **Touches:** `cli/`, `engine/factory_engine/usage.py`, tests
- **Acceptance criteria:** On a seeded registry, every grouping returns the expected totals. `--json` gives machine-readable output.
- **Tests:** Unit tests on a seeded database; CLI tests.
- **Depends on:** 2.27, 2.28

### [ ] 2.X Milestone 2 exit (manual, signed off by Kunal)

- [ ] Bootstrap run completed on the pilot project from the CLI, and merged
- [ ] 3 feature runs completed end to end from the CLI, and merged
- [ ] At least one run used feedback mid-run, and one recovered from a restart or pause
- [ ] Metrics recorded for all 4 runs (first-pass, retries, findings, tokens, your edits)
- [ ] No safety-hook or post-stage escape found during the runs, or each one fixed with a test
- [ ] Milestone summary written: what was built, what was tested, deviations from the design
- [ ] Next: give the factory its own repo as a project (`factory.yaml` for `agent-factory`)

---

## Milestones 3–7 — features and exit checks

Each of these is expanded into tasks at the start of its milestone.

### Milestone 3 — Run docs, history and split handling

- Run docs move into the repo at `docs/runs/run-<id>-<slug>/` and merge in the same PR as the code; a rejected PR lands no docs
- The run index is built by reading `docs/runs/` (no shared index file)
- The planner searches the run index and lists the past runs it used; you can point at a run ("follow how run-38…") and the engine attaches its handoff and report
- Split handling: the planner proposes parts with order and dependencies; you approve, edit (merge, drop, reorder) or reject; a parent card counts parts done; blocked parts start on updated code after their dependency merges; the parent closes when all parts merge
- Lessons: `CLAUDE.md` and `lessons.md` edits proposed inside PRs; a mistake seen twice becomes a lesson
- Replay set: store 10–15 finished tasks with accepted outcomes; a command reruns them and compares metrics
- Transcript retention: 30 days

**Exit:** a split task finishes as a parent with merged parts, with docs in `docs/runs/`.

### Milestone 4 — Multiple repos and the queue

- Registry and CLI handle several repos
- Queue with 2 concurrent agents (configurable); extra runs wait for a slot
- Runs on the same repo with overlapping plan files are queued; the actual diff is checked afterwards
- Onboarding run for existing repos: analyzer, setup (single build/test/lint commands, `CLAUDE.md`, `factory.yaml`), characterization tests, review, and your correct / bug / unknown marking per flow
- Weekly readiness re-check; limited mode (plan plus draft PR only)
- Full `litellm` provider profile: proxy preflight, per-run budget, cost recording, pause-and-ask on budget hit

**Exit:** two repos running in parallel without interference.

### Milestone 5 — HTTP API, Claude Code plugin and terminal panes

- FastAPI server on localhost only, exposing the engine actions (start, status, feedback, approve, stop, attach, usage) and a live event stream (SSE); CLI, plugin and panes share one running engine
- Plugin: slash commands `/factory:start|status|feedback|approve|attach|usage|stop`, local MCP server with the same actions, routing skill, status line (active runs, needs you, today's tokens), notification hooks
- Desktop notifications for needs you and failed
- Windows Terminal panes: one tab per run, one pane per active agent, header (run, agent, model, live tokens, state); typing sends feedback; take-over of the real session and hand-back; settings for auto-open, max panes, auto-close

**Exit:** start, steer and approve a run entirely from a Claude Code session.

### Milestone 6 — React UI

Start only after milestones 2–4 have run reliably for one to two weeks.

- Runs board: Needs you / Running / Failed / Done, cards with stage bar, attempts, tokens; filters; New run; split parent cards
- Run detail: header (status, provider, models, isolation), swimlane per agent with blocks and retries, block details, feedback box with Send to agent
- Approval view: plan, split or findings with Approve, Edit and approve, Send back with notes
- Usage and quality-metric reporting (data recorded since milestone 2)
- Shared status colours; live updates over the event stream; Playwright tests

**Exit:** board, swimlane, approvals and usage work live against a real run, with no refresh.

### Milestone 7 — Triggers

- Poll GitHub issues labelled `factory` on registered repos and start runs from them
- Link run, PR and issue; comment status back on the issue
- Jira after version 1

**Exit:** a labelled GitHub issue starts a run automatically and ends as a merge request awaiting your approval.
