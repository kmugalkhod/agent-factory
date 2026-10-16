# Plan: run-43-run-labels

## Context

Runs have no labels today. The request asks for labels on runs, a CLI filter and a board
filter. The change spans the registry, the CLI and the UI. See intent.md.

## Past runs used

None

## Files

- `engine/factory_engine/registry.py`: change, add the labels column
- `cli/factory_cli/status.py`: change, add the `--label` filter
- `ui/src/board/Filters.tsx`: change, add the label filter

## Steps

1. Add a `labels` column to the registry with a migration to schema version 3.
2. Add `--label` to `factory status`, filtering on that column.
3. Add a label filter to the runs board.

## Acceptance criteria

1. After the migration, `PRAGMA user_version` returns 3 and `runs` has a `labels` column.
2. `factory status --label bug` lists only runs labelled `bug`.
3. Choosing a label on the board shows only runs with that label.

## Test cases

- AC1: `test_migration_adds_labels`: a version 2 registry → version 3 with the column
- AC2: `test_status_filters_by_label`: runs labelled bug and docs → only the bug run
- AC3: `filters runs by label`: board with two labels → one card after choosing one

## Split

1. Registry column: add the `labels` column and its migration, depends on none
2. CLI flag: `factory status --label` filters runs by label, depends on part 1
3. Board filter: the runs board filters by label, depends on part 1 and part 2

## Questions

1. Should labels be case-sensitive? Why it matters: it decides the column's collation. Default if unanswered: case-insensitive
2. Can a run have no labels at all?
