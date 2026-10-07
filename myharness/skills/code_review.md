---
name: code_review
description: Review code changes for correctness bugs, contract violations, and security issues. Use when asked to review a file, diff, or commit.
---
Review procedure:

1. Establish scope. If reviewing uncommitted work, run `git diff` and `git status`; for a commit, `git show <rev>`. Read every changed file in full, not just the hunks — bugs often sit in unchanged code the change now depends on.
2. Check correctness first. For each change, ask what input or state makes it wrong: empty collections, None, missing keys, off-by-one, encoding, Windows paths, concurrent callers, exceptions escaping a boundary.
3. Check this repository's contracts (AGENTS.md):
   - Tool functions return str and never raise; failures are "Error: ..." strings.
   - Tool definitions and implementations live together, and every exposed tool has a handler.
   - Cross-cutting logic (permission, audit, logging, injection) is a hook, not code inside agent_loop.
   - Tool execution goes through dispatch.execute_tool().
   - tool_use / tool_result pairs are never split by trimming or compression.
   - File I/O passes encoding="utf-8"; paths are checked against the workspace.
4. Check security: untrusted model input reaching shell, filesystem, or network without validation; secrets written to logs or committed.
5. Verify before reporting. Run `pytest tests/ -q`. For each suspected bug, confirm it by reading the calling code or writing a minimal reproduction. Drop anything you cannot substantiate.

Report format: findings ordered by severity. Each finding gives file:line, the concrete failure scenario (input -> wrong result), and a suggested fix. If nothing survives verification, say so plainly. Do not edit files unless asked.
