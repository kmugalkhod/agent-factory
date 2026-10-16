# Review: run-7-login-form

Attempt: 2
Diff reviewed: 1a2b3c4..9f8e7d6

## Acceptance criteria

- AC1: met. `tests/test_login.py::test_rejects_empty_password` passes.
- AC2: met. `src/login.py:42` returns 401 for a wrong password.

## Findings

- [Minor] `src/login.py:30`: The error message repeats the user name. Fix: Drop the name from the message.
- [Nit] `src/login.py:12`: `pw` is a vague name. Fix: Rename it to `password`.

## Verdict

Verdict: approve
