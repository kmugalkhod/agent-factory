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
