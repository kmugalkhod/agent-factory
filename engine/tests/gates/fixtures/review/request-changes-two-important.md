# Review: run-7-login-form

Attempt: 1
Diff reviewed: 1a2b3c4..5d6e7f8

## Acceptance criteria

- AC1: met. `tests/test_login.py::test_rejects_empty_password` passes.
- AC2: met. `src/login.py:42` returns 401 for a wrong password.

## Findings

- [Important] `src/login.py:55`: The password is written to the log. Fix: Log the user ID only.
- [Important] `tests/test_login.py:8`: The lockout test was deleted. Fix: Restore the tester's test.
- [Minor] `src/login.py:30`: The error message repeats the user name. Fix: Drop the name from the message.

## Verdict

Verdict: request changes
