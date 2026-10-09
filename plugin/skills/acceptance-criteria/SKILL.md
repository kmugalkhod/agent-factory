---
name: acceptance-criteria
description: Use when writing or checking the Acceptance criteria and Test cases sections of plan.md, so that every criterion is numbered, measurable and covered by at least one named test.
---

# Write acceptance criteria

The acceptance criteria decide what "done" means. The tester turns each one into tests that
must fail before the build, the builder works until they pass, and the reviewer marks each one
met or not met. A vague criterion can't be tested, so it can't be enforced.

## Template

This skill fills two sections of `plan.md`: `## Acceptance criteria` and `## Test cases`. The
copy of the plan template in this folder, `plan.md`, shows their exact format. The `write-plan`
skill covers the rest of the plan.

- Acceptance criteria are a numbered list: `1.`, `2.`, ... Never bullets.
- Test cases are bullets in this form: ``- AC<n>: `<test name>`: <input or setup> → <expected result>``

## Procedure

1. List what the request needs to be true when the work is done. For a spec, take every
   requirement in it; none may be dropped.
2. Turn each item into one criterion that names something anyone can check:
   - a command and its exit code or output
   - an input and its expected output
   - a state: a file exists, a field is set, a row is written
3. Add criteria for the edges the request implies: empty input, invalid input, errors, limits.
4. Add one criterion that existing behaviour is unchanged, when the change touches code that
   already works. Its test passes before the build as well as after; that is expected.
5. For each criterion, write at least one test case that names it (`AC1`, `AC2`, ...). Give
   each test a name the tester can use as is.
6. Check the result with the questions below.

## Checks

- For new or changed behaviour: can the tester write a test that fails today and passes after
  the change? If not, the criterion isn't measurable yet. A criterion that existing behaviour
  is unchanged (step 4) is the exception: its test passes today and must still pass after.
- Does each criterion test one thing? Split "A and B" into two.
- Does every criterion have at least one test case, and does every test case name a criterion?
- Is any requirement in `intent.md` missing? Add a criterion or ask under Questions.
- Does a criterion describe how the code is built rather than what it does? Rewrite it as
  behaviour seen through a public interface.

## Examples

Vague, then measurable:

| Vague | Measurable |
| --- | --- |
| Handles bad input well | `parse("")` raises `EmptyInputError` with a message naming the file |
| Is fast | Importing 10,000 rows finishes in under 5 seconds on the test fixture |
| The API works | `GET /runs/42` returns 200 and a body whose `id` is `42` |
| Errors are clear | A missing `factory.yaml` exits 2 and prints `factory.yaml not found in <repo>` |
| Clean code | Drop it. Style belongs to the reviewer, not to a criterion |
| Supports Windows | `load("C:\\Repo\\factory.yaml")` and `load("c:/repo/factory.yaml")` return the same settings |

A filled pair of sections:

```
## Acceptance criteria

1. `slugify("Add CSV Export")` returns `add-csv-export`.
2. `slugify` keeps at most 40 characters and never ends with `-`.
3. `slugify("")` raises `ValueError`.
4. Characters outside `a-z`, `0-9` and `-` are removed: `slugify("Fix: ümlaut!")` returns `fix-mlaut`.

## Test cases

- AC1: `test_slugify_lowercases_and_joins`: "Add CSV Export" → "add-csv-export"
- AC2: `test_slugify_truncates_to_40`: a 60-character title → length 40, last character not "-"
- AC2: `test_slugify_strips_trailing_dash_after_cut`: title whose 40th character is a space → no trailing "-"
- AC3: `test_slugify_empty_raises`: "" → ValueError
- AC4: `test_slugify_drops_other_characters`: "Fix: ümlaut!" → "fix-mlaut"
```

## Common mistakes

- Criteria that restate the steps ("add a function `slugify`"). Say what it must do instead.
- Numbers without a fixture or a setup. "Under 5 seconds" needs the input it is measured on.
- One test case covering several criteria with no name per criterion. The reviewer then
  can't tell which criterion a failing test belongs to.
- Leaving out the unhappy paths. Most review findings are about errors and edges.
