# Review: run-<id>-<slug>

Attempt: <n>
Diff reviewed: <base commit>..<head commit>

## Acceptance criteria

<Every criterion from plan.md, and every requirement in intent.md that the plan doesn't cover.
Mark each met or not met, with the evidence: the test, file or line that shows it.>

- AC1: <met | not met>. <evidence>

## Findings

<One line per finding, most severe first. "None" if there are none.
Important: must be fixed before merge (wrong behaviour, a criterion not met, a safety or data
risk, a weakened test). Minor: should be fixed, but doesn't block. Nit: style or naming.>

- [Important] `<path>:<line>`: <problem>. Fix: <what to change>
- [Minor] `<path>:<line>`: <problem>. Fix: <what to change>
- [Nit] `<path>:<line>`: <problem>. Fix: <what to change>

## Verdict

<Exactly one line. "request changes" if any finding is Important or any criterion is not met;
otherwise "approve".>

Verdict: <approve | request changes>
