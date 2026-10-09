---
name: review-rubric
description: Use when you are the reviewer and must check a run's diff against intent.md and plan.md and write review.md, with every acceptance criterion marked, findings graded Important, Minor or Nit, and one verdict line.
---

# Review rubric

Your review decides whether the change reaches a human or goes back to the builder. The
reviewer gate fails on any `[Important]` finding, so be precise: block what must be fixed and
nothing else.

## Template

This folder holds `review.md`, an exact copy of the factory's review template. Copy it to
`review.md` in the run folder and fill it in. It is your only output and also your handoff.

- Fill the title line, `Attempt:` and `Diff reviewed:` (the base and head commits the engine
  gave you).
- Keep the three `## ` headings, in order: Acceptance criteria, Findings, Verdict.
- At most 60 lines. No code fences. Replace every `<angle bracket>` placeholder.

## Procedure

1. Read `intent.md`, `plan.md`, and `handoff-planner.md`, `handoff-tester.md` and
   `handoff-builder.md`.
2. Read the whole diff the engine gave you, then the files it touches, then the repo's
   `CLAUDE.md` and `docs/lessons.md`.
3. Work through the checklist below. Write each finding down as you find it.
4. Mark every acceptance criterion met or not met, with evidence.
5. Sort the findings, most severe first, and write the verdict.

## Checklist

- **Acceptance criteria.** Each criterion in `plan.md` is met, shown by a test that checks it,
  a file or a line. A test that exists but doesn't check the criterion doesn't count.
- **Intent.** Every requirement in `intent.md` is covered, including ones the plan missed. List
  a missed requirement under Acceptance criteria as not met.
- **Tests.** The tester's tests are unchanged by the builder, test what they claim, and can
  fail. Watch for asserts that always pass, skipped tests and over-broad mocks.
- **Scope.** Files changed outside the plan's Files list. Fine if the builder's handoff explains
  why; a finding if not.
- **Correctness.** Wrong behaviour, unhandled errors, edge cases the criteria imply, resources
  left open, off-by-one limits.
- **Safety.** Secrets or tokens in code or logs, paths outside the repo, shell commands built
  from strings, data deleted or overwritten without a check.
- **Fit.** The conventions in `CLAUDE.md` and the surrounding code: naming, error handling,
  layout.

## Severities

| Severity | Use for | Effect |
| --- | --- | --- |
| Important | Wrong behaviour, a criterion not met, a dropped requirement, a weakened test, a safety or data risk | Blocks: request changes |
| Minor | Should be fixed but doesn't break anything: a confusing name in a public interface, a missing log line, a small duplicate | Doesn't block |
| Nit | Style or naming only | Doesn't block |

When unsure between two severities, ask: would merging this cause a bug, a security problem or
a broken promise in the plan? If yes, Important. If no, Minor at most.

## Line formats

The gate parses these lines, so copy the format exactly:

- Criterion: `- AC<n>: <met | not met>. <evidence>`
- Finding: ``- [Important] `<path>:<line>`: <problem>. Fix: <what to change>``, with
  `Important`, `Minor` or `Nit` in the brackets.
- Findings section with none: the single line `None`.
- Verdict: exactly one line, `Verdict: request changes` if any finding is Important or any
  criterion is not met, otherwise `Verdict: approve`.

## Example

```
# Review: run-42-status-json

Attempt: 1
Diff reviewed: 3f2a1c0..9b7e4d2

## Acceptance criteria

- AC1: met. `test_status_json_parses` loads the output with `json.loads`.
- AC2: met. `test_status_json_keys` compares the key set exactly.
- AC3: not met. `cli/factory_cli/status.py:58` returns `None` for an empty registry, printed as `null`.
- AC4: met. `test_status_table_unchanged` passes against the saved table.

## Findings

- [Important] `cli/factory_cli/status.py:58`: an empty registry prints `null`, not `[]` (AC3). Fix: return an empty list when there are no runs.
- [Minor] `cli/factory_cli/status.py:41`: `--json` and `--wide` together are accepted silently. Fix: document that `--json` ignores `--wide` in the option help.
- [Nit] `cli/factory_cli/status.py:12`: `d` is unclear. Fix: rename to `run_dict`.

## Verdict

Verdict: request changes
```

## Common mistakes

- A finding without a path and line. The builder can't find what you mean.
- A finding without a Fix. Say what to change, not only what is wrong.
- Grading a style preference as Important. It sends the run back for nothing.
- Approving with a criterion marked not met. That is always `request changes`.
- Two verdict lines, or a verdict line with extra words. The gate reads exactly one.
