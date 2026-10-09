# Plan: run-<id>-<slug>

## Context

<Two to five sentences: what the request asks for, what exists in the code today, and what
changes. Point to intent.md for the full request.>

## Past runs used

<Each past run you read and what you took from it, or "None".>

- run-<id>-<slug>: <what was reused or avoided>

## Files

<Every file to create or change, one per line, with the reason. Tests go under the tester's paths.>

- `<path>`: <create | change>, <why>

## Steps

<Ordered steps the builder follows. Each step is small enough to check on its own.>

1. <step>

## Acceptance criteria

<Numbered. Each is measurable: a command, an input and its expected output, or a state you can
check. These become the tests, so avoid "works well", "is fast" or "is clean".>

1. <criterion>

## Test cases

<At least one test per acceptance criterion. Name the criterion each test covers.>

- AC1: `<test name>`: <input or setup> → <expected result>

## Split

<Optional. Only when the change is over about 400 changed lines or 10 files. Otherwise delete
this section. List the parts in order, with dependencies.>

1. <part title>: <scope>, depends on <none | part N>

## Questions

<Optional. Only when something in intent.md is unclear or conflicts with the code. Otherwise
delete this section. Each question says why it matters and what you would assume.>

1. <question>. Why it matters: <reason>. Default if unanswered: <assumption>
