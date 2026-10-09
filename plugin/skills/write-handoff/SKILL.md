---
name: write-handoff
description: Use when you are the planner, tester or builder and have finished your stage, to write handoff-<role>.md in the run folder for the roles that come after you. The reviewer does not use it; review.md is its handoff.
---

# Write a handoff

The next agent starts with no memory of your session. Your handoff is the only place it learns
what you changed, what you decided and what you left open. Write it for an agent that will
trust it.

## Template

This folder holds `handoff.md`, an exact copy of the factory's handoff template. Copy it to your
own file in the run folder and fill it in:

| Role | File |
| --- | --- |
| planner | `handoff-planner.md` |
| tester | `handoff-tester.md` |
| builder | `handoff-builder.md` |

- Fill the title line: `# Handoff: <role>, run-<id>-<slug>`, and the `Attempt:` line. The
  planner and tester write `Attempt: 1`; the builder writes the attempt number the engine gave.
- Keep all four `## ` headings, in order: Files touched, Decisions, Rejected options, Gaps.
  A section with nothing in it says "None"; the heading stays.
- Replace every `<angle bracket>` placeholder, including the guidance under each heading.
- At most 60 lines. No code fences.
- The builder rewrites `handoff-builder.md` from scratch on every attempt. The engine has
  already archived the earlier one, so don't append to it.

## Procedure

1. Before you start your stage, read the handoffs of every earlier role. Your handoff builds on
   theirs; don't repeat what they already said.
2. While you work, note each file you create, change or delete, and each choice you make.
3. When your stage's work is done, copy the template and fill each section as below.
4. Reread it as the next role: could it start work without asking you anything?

## Sections

**Files touched.** Every file you created, changed or deleted, one per line, with what changed.
Take it from your own record of edits. The planner lists `plan.md`.

**Decisions.** Choices the next stage needs, each with its reason: a library picked, a name
chosen, a plan step read one way rather than another, a file changed that the plan didn't list.

**Rejected options.** Approaches you tried or considered and dropped, and why, so the next role
doesn't try them again.

**Gaps.** What is not done, not tested or unsure, and what the next role should check first.
Write "None" only if there is truly nothing.

The tester and the builder also add the test command they ran and its result, as the last line
under Gaps or Decisions.

## Example

A builder's handoff after a second attempt:

```
# Handoff: builder, run-42-status-json

Attempt: 2

## Files touched

- `cli/factory_cli/status.py`: changed, added `--json` and a `runs_as_dicts` helper

## Decisions

- Field order follows the table columns: plan step 2 didn't fix one, run-31 used this order.
- `--json` ignores `--wide`: the JSON already holds every field.

## Rejected options

- Serialising the pydantic models directly: they carry internal fields the plan excludes (AC2).

## Gaps

- Attempt 1 failed AC3 (`[]` printed as `null`); fixed by returning a list for an empty registry.
- Last run: `uv run pytest cli/tests` → 14 passed, 0 failed.
```

A tester's Gaps section:

```
## Gaps

- AC4 compares against a saved table in `cli/tests/data/status_table.txt`; regenerate it only
  if the plan changes the table.
- Test run before the build: `uv run pytest cli/tests` → 4 failed (AC1–AC4, missing `--json`), 10 passed.
```

## Common mistakes

- "Updated files as planned." Name each file and what changed in it.
- Decisions without a reason. The reviewer can't judge a choice it can't follow.
- Hiding a failing or skipped test. Put it under Gaps; the engine and reviewer find it anyway.
- Writing the handoff before the work is finished, then changing more files afterwards.
