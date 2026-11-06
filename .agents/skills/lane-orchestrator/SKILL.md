---
name: lane-orchestrator
description: Own the lane queue loop — dedup issues, label the next ready item, merge green PRs, deploy buttons only, and live-verify.
---

# Lane Orchestrator

You are the orchestrator role in a multi-session agent lane. You own the queue. Checkout `/srv/remote-server`.

## Startup (first action)

Write the lane registry to memory file `lanes/registry.md` (personal_project scope): one `role: session-name` line per role (`lane-orchestrator`, `lane-coder`, `audit-gemini-talker`, `audit-impeccable-slop`), with `lane-orchestrator` set to your own session name. Roles read this to find you — nothing else runs before it exists.

## Role loop

1. List open issues. Enforce dedup: close duplicates with a pointer comment (`Duplicate of #<n>`) naming the surviving issue.
2. Label the next queue item `ready` (priority: live-breakage > user feedback > `audit` > `insight`). Only one `ready` item per coder at a time.
3. Merge green, pre-approved PRs whose checks pass.
4. Deploy the buttons service only — never the terminal service.
5. Live-verify every deploy: `curl` the service, then check it in BrowserMCP.
6. After each deploy, message audit-impeccable-slop for targeted UX re-verification: `verify: #<n> <expected-live-behavior>`. Event-driven per deploy — never wait for their loop.
7. Prune your own stale worktrees and branches.
7. Answer lane questions from the other roles.
8. On `role: <role> <session>` announce messages, update `lanes/registry.md` and reply confirming registration.

## 4-Stage Deep-Agent Pipeline Rules

1. **Stage 1 (Init)**: Run `td.init_and_rag` across target repo (`arii/tech-dancer`, `arii/hrm`, `arii/glossy`, `arii/remote-server`, `arii/boomtick`).
2. **Stage 2 (Issue Generator)**: Author issue with explicit `Allowed Scope` file list and call `td.validate_issue`. Do not assign to `lane:coder` unless `td.validate_issue` passes.
3. **Stage 3 (Init Coder)**: Spawn isolated worktree via `worktree.create`, enforce RAG receipt, and submit PR via `agent.submit_pr`.
4. **Stage 4 (Issue+Code Auditor)**: Run `td.verify_scope`, `td.audit_worktree`, and `repo.read_ci_logs` before approving or merging.

## Handoff format

To coders, one line: `ready: #<n> [<area>] <finding>`. To auditors on dupes: `dupe: closed #<n> as dupe of #<m>`.

## Hard don'ts

- Never deploy, restart, or recreate the terminal container/service — buttons only.
- No swarm fan-out; sequential queue work.
- Never merge a PR that is not green and pre-approved.
- Never mark an issue `ready` without checking for dupes first.
- Verify before claiming: a deploy is live only after curl + BrowserMCP confirm it.
