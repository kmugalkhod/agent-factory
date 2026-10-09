---
name: write-plan
description: Use when you are the planner and must write plan.md for a run, turning intent.md into files, steps, acceptance criteria and test cases that the tester, builder and reviewer follow.
---

# Write a plan

The plan is the contract for the rest of the run. The tester writes tests from it, the builder
changes only the files it lists, and the reviewer checks the diff against it. The planner gate
checks that it names files, steps and measurable acceptance criteria.

## Template

This folder holds `plan.md`, an exact copy of the factory's plan template. Copy it to `plan.md`
in the run folder and fill it in. Don't write the file from memory.

- Keep every `## ` heading, in order, spelled exactly as in the template. Gates read them.
- Replace every `<angle bracket>` placeholder, including the guidance under each heading.
- Delete `## Split` and `## Questions` entirely, heading included, when they don't apply.
- At most 60 lines. No code fences: gates read the file line by line.

## Procedure

1. Read `intent.md` in full, then the repo's `CLAUDE.md` and `docs/lessons.md` if present.
2. Search `docs/runs/` for past runs in the same area or named in `intent.md`. Read their
   `plan.md`, handoffs and `report.md`.
3. Read the code the request touches until you can name every file that has to change.
4. If `intent.md` holds a spec, list each requirement in it. Every one must end up as an
   acceptance criterion or a question.
5. Fill the sections in the order below.
6. Count the files and estimate changed lines. Above about 400 lines or 10 files, add a Split.
7. Reread the plan as the builder would: could you follow each step without asking?

## Sections

**Context.** Two to five sentences: what is asked, what exists today, what changes. Point to
`intent.md` for the full request rather than copying it.

**Past runs used.** One line per past run you read and what you took from it or avoided. Write
"None" when there were none; the heading stays.

**Files.** Every file to create or change, one per line, with `create` or `change` and why.
Include the test files the tester will write, under the tester's paths. The builder may touch
only what is listed here, so leave nothing out.

**Steps.** Numbered, in order. Each step is small enough for the builder to check on its own,
and says which file it touches.

**Acceptance criteria** and **Test cases.** Use the `acceptance-criteria` skill. Each criterion
is numbered and measurable; each test case names the criterion it covers.

**Split** (optional). Only above about 400 changed lines or 10 files. List the parts in order,
each small enough for one merge request, with its dependencies.

**Questions** (optional). Only when something in `intent.md` is unclear or conflicts with the
code. Each question says why it matters and what you will assume if it goes unanswered.

## Example

A filled plan for a small request, "add a `--json` flag to `factory status`":

```
# Plan: run-42-status-json

## Context

`factory status` prints a table of runs. The request asks for a `--json` flag that prints the
same runs as JSON for scripts. Only the CLI's status command and its tests change. See intent.md.

## Past runs used

- run-31-status-table: reused its column order as the JSON field order

## Files

- `cli/factory_cli/status.py`: change, add the flag and the JSON output
- `cli/tests/test_status.py`: change, tests for the flag (tester)

## Steps

1. In `status.py`, add a `--json` option to the `status` command, default off.
2. When set, print a JSON list with one object per run: id, slug, stage, status.
3. Keep the table output unchanged when the flag is absent.

## Acceptance criteria

1. `factory status --json` exits 0 and prints a JSON list that `json.loads` parses.
2. Each object has exactly the keys `id`, `slug`, `stage`, `status`.
3. With no runs, `factory status --json` prints `[]`.
4. `factory status` without the flag prints the same table as before.

## Test cases

- AC1: `test_status_json_parses`: two runs in the registry → output parses as a list of 2
- AC2: `test_status_json_keys`: one run → its object has exactly the four keys
- AC3: `test_status_json_empty`: empty registry → output is `[]`
- AC4: `test_status_table_unchanged`: two runs, no flag → output equals the saved table
```

No Split or Questions here: two files, a clear request.

## Common mistakes

- Steps that say "implement the feature": split them until each is checkable.
- A file the builder will need that isn't under Files. The builder then has to stop or explain.
- Test files missing from Files. The tester's paths are part of the plan too.
- Questions you could answer by reading the code. Read it instead.
- Planning work the request didn't ask for. Put it under Gaps in your handoff as a follow-up.
