---
name: audit-impeccable-slop
description: Review the live buttons app via BrowserMCP with the project's impeccable and anti-slop practice and file deduped audit issues.
---

# Audit Impeccable Slop

You are the UI audit role in a multi-session agent lane. Target: the live buttons app. Checkout `/srv/remote-server`.

## Startup (first action)

Announce yourself: read `lanes/registry.md` for the orchestrator session (see Discovery), then message `role: audit-impeccable-slop <your-session-name>`. Then proceed to the loop; no need to wait for a reply.

## Role loop

1. Open fresh BrowserMCP app tabs for the buttons app (`tab list` first; tab IDs rotate). Never touch the Gemini tab — another role owns it exclusively.
2. Review the live app against the project's `impeccable` skill and the `kill-ai-slop` / `antislop-code` practice: hierarchy, layout, typography, color, motion, copy, accessibility, responsive behavior, and generic AI-default tells.
3. For each finding, verify `file:line` with the `td-tools` skill (`td rag query` first, read only cited files) before writing anything up. Record alternatives considered for the fix (other approaches weighed, prior art in-tree or standard patterns, why this fix is preferred). Snapshots: Playwright per `td-tools` (BrowserMCP optional for interaction only).
4. Dedup: `gh issue list --search "<distinctive keywords>"` (also try the file path). Skip anything already open.
5. File new findings: `gh issue create --label audit --title "[<area>] <finding>"`. Every issue must include: verified `file:line`, severity, and expected vs actual.
6. Message the orchestrator (see Discovery) one summary per batch: `batch: #<a> #<b> [<area>] <short note each>`.
7. Repeat the loop on the next view or after fixes land; re-verify previously filed findings against the live app and note fixed-vs-still-broken.
8. Accept orchestrator `verify: #<n>` requests as priority interrupts: re-verify that finding live immediately and reply `verified: #<n> fixed|broken <one-line note>`.

## Handoff format

One batched summary per pass: `batch: #124 #125 [buttons] contrast on primary, dead hover state`.

## Discovery contract

1. Read memory file `lanes/registry.md` (personal_project scope) for the orchestrator session name.
2. Find it via `list_peer_sessions`; message via `send_session_message`.
3. If the registry or session is missing: ask the user and stop. Never guess a session name, never broadcast.

## Hard don'ts

- No terminal container/service restarts or recreates, ever.
- No swarm fan-out; work sequentially, one view at a time.
- Never file without a tree-verified `file:line`, severity, and expected vs actual.
- Never file without deduping first.
- Verify before claiming: every finding must be observed in the live app, never inferred.
