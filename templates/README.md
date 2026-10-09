# Run document templates

Every stage of a run writes one of these files and the next stage reads it. The engine's gates
parse them, so the headings below are a contract.

**Rules**

- This file is the single list of headings. `tools/tests/test_templates.py` checks that each
  template has exactly these `## ` headings, in this order.
- Changing a heading means changing this list, the template, and the gate that parses it (from
  milestone 2), in the same commit.
- Each template starts with one `# ` title line. Only `## ` headings are part of the contract.
- A filled document keeps every required heading, even when its section is "None". Delete an
  optional section entirely, heading included, when it doesn't apply.
- Text in `<angle brackets>` is guidance or a placeholder; replace it.
- Each document fits on one page: at most 60 lines. No code fences, since gates read headings
  line by line.

## intent.md

Written by you (or the intake step) before the planner. A spec goes in unchanged.

- `Request`
- `Answers`

## plan.md

Written by the planner. The planner gate checks that it names files, steps and measurable
acceptance criteria.

- `Context`
- `Past runs used`
- `Files`
- `Steps`
- `Acceptance criteria`
- `Test cases`
- `Split` (optional)
- `Questions` (optional)

## handoff.md

Written by every role at the end of its stage.

- `Files touched`
- `Decisions`
- `Rejected options`
- `Gaps`

## review.md

Written by the reviewer. The reviewer gate fails on any `[Important]` finding.

- `Acceptance criteria`
- `Findings`
- `Verdict`

Line formats the gate parses:

- Finding: ``- [<Important | Minor | Nit>] `<path>:<line>`: <problem>. Fix: <change>``
- Verdict: exactly one line, `Verdict: approve` or `Verdict: request changes`

## report.md

Written at the end of the run, for you and for later runs.

- `Summary`
- `Acceptance criteria`
- `Tests`
- `Review`
- `Run`
- `Follow-ups`
