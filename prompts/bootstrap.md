# Bootstrap addendum

Added to the planner and builder prompts for a bootstrap run (run 0) on a new, empty project. A
bootstrap run makes the repo ready for feature runs. Feature runs depend on tests to check their
own work, so without them every gate would pass on its own.

The repo is ready when:

- build, test and lint are each one command, and all three pass
- a smoke test exists and passes: the app starts, or a health endpoint returns 200
- `CLAUDE.md` and `factory.yaml` exist
- if there is a UI, a browser test with Playwright exists and passes

## Planner

- Propose the stack, folder layout and test setup in `plan.md`. Put the stack and the reason for
  it under Context.
- If `intent.md` doesn't settle the language or framework, ask under Questions. Give your
  recommendation as the default.
- Files include the project skeleton, the test framework config, the smoke test, `CLAUDE.md` and
  `factory.yaml`.
- The acceptance criteria are the readiness items above, with the exact build, test, lint and
  smoke commands.
- Don't plan features. They come later as normal runs, after this plan merges.
- Don't propose a split. The skeleton is one merge request.

## Builder

- There is no tester stage in a bootstrap run. Skip the base builder prompt's reads of
  `handoff-tester.md` and the tests the tester wrote. You write the test framework config and the
  smoke test yourself, so your write paths cover the whole worktree, except `plan.md`.
- The repo's `CLAUDE.md` and `docs/lessons.md` don't exist yet either. Skip those reads too; you
  write `CLAUDE.md` in this run.
- Create the skeleton: dependencies, config, the test framework, one passing smoke test, and
  Playwright tests if there is a UI.
- Write `CLAUDE.md`: the build, test and lint commands, the folder layout, the conventions, and
  anything agents get wrong.
- Write `factory.yaml` with the test, build, lint and smoke commands, and the allowed write paths
  per role for this layout.
- Before you finish, run build, test and lint, and the smoke test. All must pass. Put the commands
  and results in `handoff-builder.md`.
