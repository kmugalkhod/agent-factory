# Milestones

Status of each milestone. Task detail is in [task.md](task.md); the design is in [../design/design.md](../design/design.md).

Status values: `Not started`, `In progress`, `Awaiting exit sign-off`, `Done`.

| # | Milestone | Status | Tasks | Exit check |
| --- | --- | --- | --- | --- |
| — | Setup before development | Not started | S.1–S.6 (manual) | All required setup items ticked |
| 1 | Prompts and templates by hand | Not started | 1.1–1.5, 1.X | The manual result is good enough to merge |
| 2 | Engine and CLI, one repo | Not started | 2.1–2.29, 2.X | Bootstrap plus 3 feature runs completed end to end from the CLI |
| 3 | Run docs, history and split handling | Not started | To be expanded | A split task finishes as a parent with merged parts, docs in `docs/runs/` |
| 4 | Multiple repos and the queue | Not started | To be expanded | Two repos running in parallel without interference |
| 5 | HTTP API, Claude Code plugin and terminal panes | Not started | To be expanded | Start, steer and approve a run entirely from a Claude Code session |
| 6 | React UI | Not started | To be expanded | Board, swimlane, approvals and usage work live against a real run, with no refresh |
| 7 | Triggers | Not started | To be expanded | A labelled GitHub issue starts a run automatically and ends as a merge request awaiting approval |

Milestone 6 starts only after milestones 2–4 have run reliably for one to two weeks.

## Log

| Date | Milestone | Change |
| --- | --- | --- |
| 2026-10-09 | — | Task plan created; milestones 1–2 detailed, 3–7 listed as features |
