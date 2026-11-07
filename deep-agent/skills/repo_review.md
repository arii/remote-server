---
name: repo_review
description: Systematic read-only code review with the repo tools. Use when the user asks for a review of code, a PR, or a change.
trigger_nodes:
  - intent_review
---

# Repo Review

## Overview
Review code using the repo tools (`list_dir`, `grep_repo`,
`read_repo_file`) plus `search_docs` for project conventions.
Never propose writes unless asked.

## Tool Warning
The harness also exposes file tools (`ls`, `read_file`, `glob`,
`grep`, `write_file`, `edit_file`) — those see a VIRTUAL
thread-scoped filesystem, NOT the checkout. For repo reads always
use the `*_repo` / `list_dir` tools; never trust virtual-fs
content as repo state.

## When to Use
Review intent turns: "review X", "look over PR N", "is this change
safe". The loader attaches this skill automatically.

## Instructions
1. Locate the change: `grep_repo` for the symbols under review;
   `read_repo_file` each touched file in full before judging.
2. Trace every caller and every test of the changed functions.
   Untested behavior is a finding, not an assumption.
3. Check the repo's own conventions first (`AGENTS.md` via
   `search_docs`): match existing patterns, don't invent new ones.
4. Report findings ordered by severity: correctness, then
   safety/perf, then style. Each finding names file, line, and the
   concrete failure or risk — no vague praise, no filler.
5. End with a verdict: approve, approve-with-nits, or
   needs-changes (with the blocking items listed).
