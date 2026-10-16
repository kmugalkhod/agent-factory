# Review: run-7-login-form

Attempt: 1
Diff reviewed: 1a2b3c4..5d6e7f8

## Acceptance criteria

- AC1: met. `tests/test_login.py::test_rejects_empty_password` passes.
- AC2: not met. `src/login.py:42` returns 200 for a wrong password.

## Findings

- [Important] `src/login.py:42`: A wrong password logs the user in. Fix: Compare the hash and return 401 on a mismatch.
- [Nit] `src/login.py:12`: `pw` is a vague name. Fix: Rename it to `password`.

## Verdict

Verdict: request changes
