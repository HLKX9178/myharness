---
name: debug
description: Systematically find the root cause of a failing test, crash, or wrong output. Use when something is broken and the cause is not obvious.
---
Debugging procedure:

1. Reproduce. Get the exact failing command and its full output (`pytest tests/path::test_name -q`, or the CLI invocation). If you cannot reproduce it, stop and report what you tried.
2. Read the error bottom-up. Locate the innermost frame inside this repository and read that function in full.
3. Form one hypothesis at a time, stated as "X happens because Y". Pick the cheapest experiment that would disprove it: add a print, write a 5-line script, or inspect state with a targeted test.
4. Narrow, don't guess. If an experiment surprises you, update the hypothesis instead of piling on fixes. After two failed fixes for the same hypothesis, discard it and return to step 2.
5. Fix the cause, not the symptom. Do not catch-and-ignore exceptions, loosen assertions, or special-case the failing input to make the error disappear.
6. Confirm. Re-run the original failing command, then the full suite (`pytest tests/ -q`). Add a regression test that fails without the fix.
7. Clean up any temporary prints or scratch files you created.

Report: the root cause in one or two sentences, the fix, and how it was verified.
