# Run document templates

Every stage of a run writes one of these files and the next stage reads it. The engine's gates
parse them, so the headings below are a contract.

**Rules**

- This file is the single list of headings. `tools/tests/test_templates.py` checks that each
  template has exactly these `## ` headings, in this order.
- Changing a heading means changing this list, the template, the copies bundled with the
  factory skills in `plugin/skills/`, and the gate that parses it (from milestone 2), in the
  same commit. `tools/tests/test_skills.py` checks that each bundled copy equals its template.
- Each template starts with one `# ` title line. Only `## ` headings are part of the contract.
- A filled document keeps every required heading, even when its section is "None". Delete an
  optional section entirely, heading included, when it doesn't apply.
- Text in `<angle brackets>` is guidance or a placeholder; replace it.
- Each template, and each document an agent or the engine writes from one, fits on one page: at
  most 60 lines. No code fences, since gates read headings line by line.
- Supplied content is the exception. The body of `intent.md`'s `Request` section is the request
  or spec exactly as you gave it: never shortened, reworded or reformatted, whatever its length,
  fences or headings. Gates treat it as opaque text (see intent.md below).

## intent.md

Written by you (or the intake step) before the planner. A spec goes in unchanged.

- `Request`
- `Answers`

The `Request` body may contain any Markdown, including its own `## ` lines. Gates look only at
the first `## Request` line and the last `## Answers` line; everything between them is the
request. Answers are appended after `## Answers`, so it stays the last heading.

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

Written at the end of its stage by the planner, the tester and the builder, each to its own
file in the run folder: `handoff-planner.md`, `handoff-tester.md`, `handoff-builder.md`. The
reviewer writes `review.md` only.

- Before starting, each role reads the handoffs of every earlier role. For example, the builder
  reads `handoff-planner.md` and `handoff-tester.md`.
- `handoff-builder.md` holds the latest builder attempt. Before the next attempt overwrites it,
  the engine copies it to `attempts/handoff-builder-<n>.md` in the run folder.

- `Files touched`
- `Decisions`
- `Rejected options`
- `Gaps`

## review.md

Written by the reviewer. The reviewer gate fails on any `[Important]` finding. `review.md` holds
the latest review; before the next review overwrites it, the engine copies it to
`attempts/review-<n>.md` in the run folder.

- `Acceptance criteria`
- `Findings`
- `Verdict`

Line formats the gate parses:

- Finding: ``- [<Important | Minor | Nit>] `<path>:<line>`: <problem>. Fix: <what to change>``
- Verdict: exactly one line, `Verdict: approve` or `Verdict: request changes`

## report.md

Written at the end of the run, for you and for later runs.

- `Summary`
- `Acceptance criteria`
- `Tests`
- `Review`
- `Run`
- `Follow-ups`
