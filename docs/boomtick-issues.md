# BoomTick-side issues (tracked from remote-server)

Separation of concerns: fixes in `arii/BoomTick` (MCP server `mcp/` + `td` CLI
`cli/`) cannot be pushed by the GitHub connector (403 on that repo — writes go
through Ari or his local agent). This file tracks them here so nothing is lost.
Verified against BoomTick `main` on 2026-10-29 unless noted.

## Open

### #1010 — `jules.list_sessions` returns raw array, breaks structuredContent validation
- **State:** OPEN (filed by Ari 2026-10-29, label `jules`, 2 comments).
- **Symptom:** `MCP error -32602: Invalid tools/call result: structuredContent:
  Invalid input: expected record, received array`.
- **Cause:** `listJulesSessionsHandler` (`mcp/src/tools/jules/list-sessions.ts`)
  returns `Promise<JulesSession[]>` — a bare array — while the declared
  `JulesListSessionsResponseSchema` is `z.object({ sessions: ... })`.
  Checked the other jules handlers: only `list_sessions` returns a raw
  array; the rest return objects.
- **Fix (patch ready, verified `git apply --check` clean):**
  `~/workspace/boomtick-1010-fix/fix-1010-list-sessions-wrap.patch` —
  apply with `git apply` in a BoomTick checkout. Wraps the return as
  `{ sessions: [...] }`, matching the schema and the issue's remediation.
- **Why it matters here:** buttons "Sync & nudge agents" calls
  `td agent list-sessions` → `jules.list_sessions` via MCP. Until fixed, the
  Jules half of that button fails. (The missing-`JULES_API_KEY` graceful
  handling is a separate buttons-side item — done in 6a201762.)

## Resolved (verified 2026-10-29)

### Legacy `repo://agent-contract` URI removed
- `mcp/src/mcp/definitions.ts` and `mcp/src/mcp/server.ts` on BoomTick `main`
  no longer reference `repo://agent-contract`; canonical URI is
  `mcp://workspace/agent-contract` (underscore alias
  `mcp://workspace/agent_contract` still handled in `server.ts`, not listed).
- The local patch `~/workspace/boomtick-legacy-fix/remove-legacy-agent-contract-uri.patch`
  is obsolete — do not apply. No push needed.
- `terminal/pi-config/APPEND_SYSTEM.md` on remote-server `main` documents the
  removal and pins the server name (`server: "boomtick"`, never URI tokens).

### v0.15 aggregate closed the earlier backlog
- BoomTick `main` has exactly one open issue (#1010). The previously tracked
  #986 (multi-repo context), #990 (MCP `create_branch` cwd bug), #983 (docs)
  are closed — v0.15 merges (#995 review runner hardening, #997 CLI DAG,
  #998 MCP modernization, #1001 agent onboarding + multi-repo context +
  sequential orchestration, #1002 pure-node container) landed.
- Unfiled draft: backslash-in-f-string `SyntaxError` in the orchestrator —
  precise scan of `main` found zero instances; treat as fixed.

## Proposed (from the UX evaluation guide §2 — not filed as issues)

MCP server (`mcp/`):
- Typed `outputSchema` + `structuredContent` on remaining tools (enforcement is
  now active — see #1010; finish the migration instead of string payloads).
- Review action: emit native GitHub `suggestion` blocks so PR authors can
  accept with one click.
- AST-aware diff filtering / token budgeting: drop lockfiles, minified builds,
  binaries/SVGs from review prompts.

`td` CLI (`cli/`):
- `td audit`: tree-sitter checks for raw `<div style=...>`, unapproved
  `@ts-ignore`, AI placeholder stubs. Keep `td` LLM-free.
- Unified `td preflight` (typecheck + `git merge-tree` conflict simulation +
  PR branch overlap scan).

Release:
- Component versioning (`ghcr.io/arii/boomtick-review`, `@arii/boomtick-mcp`,
  `boomtick-cli`) via release-please config.

## Watch list
- Import-time config crash draft (from 2026-10-28 notes): never verified, never
  filed. Re-check if `td` cold-start failures appear in buttons logs.
