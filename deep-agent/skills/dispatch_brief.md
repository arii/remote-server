---
name: dispatch_brief
description: How to scope an implementation or repair brief for propose_action(kind='dispatch'). Use when the turn will propose a worktree session.
trigger_nodes:
  - intent_implement
  - intent_fix
---

# Dispatch Brief

## Overview
A dispatch brief becomes a coder's work order. Write it so a fresh
worktree session can execute without asking questions. Ground every
claim with the repo tools (`list_dir`, `grep_repo`,
`read_repo_file`) first — not the harness file tools, which see a
virtual thread-scoped filesystem instead of the checkout.

## When to Use
Implement and fix intent turns that end in a `dispatch` proposal.
The loader attaches this skill automatically.

## Instructions
1. Scope before proposing: `grep_repo`/`read_repo_file` the exact
   files and functions involved. Name them in the brief.
2. TASK structure, in order: goal (one sentence), in-scope files,
   explicit non-goals, verification commands (repo's own tests),
   acceptance criteria (observable, no judgment calls).
3. Keep the brief under 4000 chars. One work item per proposal;
   split follow-ups into separate turns.
4. Never include secrets, absolute user paths, or model names.
   Branch: `main` unless the user named one.
