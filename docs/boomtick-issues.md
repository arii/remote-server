# BoomTick-side issues (tracked from remote-server)

Separation of concerns: fixes in `arii/BoomTick` (MCP server `mcp/` + `td` CLI
`cli/`) cannot be pushed by the GitHub connector (403 on that repo — writes go
through Ari or his local agent). This file tracks them here so nothing is lost.
Verified against BoomTick `main` on 2026-10-29.

## Open

### Unfiled — `td preflight` runs BoomTick-repo checks on every repo
- **State:** UNFILED (found 2026-10-29 from the S5 ledger).
- **Symptom:** `td --json preflight` in `/srv/remote-server` fails: `pnpm run
  type-check` → `ERR_PNPM_NO_SCRIPT`, `pnpm run lint` → `ERR_PNPM_NO_SCRIPT`,
  `node scripts/detect-antipatterns.mjs` → `MODULE_NOT_FOUND`. The buttons
  "Preflight check" button is red for remote-server for the wrong reason.
- **Cause:** `Orchestrator.run_preflight` (`cli/dev_tools/orchestrator.py`)
  hardcodes the BoomTick pnpm monorepo checks — it never checks whether the
  current repo has those scripts. `has_errors` then flips the whole report
  to `status: error`.
- **Fix (patch ready, applies clean, py_compile clean):**
  `~/workspace/boomtick-preflight-skip/fix-preflight-skip-missing-checks.patch`
  — apply with `git apply` in a BoomTick checkout. Adds `_pnpm_has_script()`;
  typecheck/lint/audit report `{"status": "skipped", "reason": ...}` when the
  repo lacks the script, instead of erroring. `has_errors` already only
  counts `"error"`, so skipped checks don't fail the report.
- **Why it matters here:** the buttons preflight digest surfaces these as
  failures; after the patch, remote-server preflight degrades to the checks
  that exist (doctor, overlaps, merge simulation).

## Resolved (verified 2026-10-29)

### #1010 — `jules.list_sessions` returns raw array, breaks structuredContent validation
- **State:** RESOLVED / CLOSED ([Issue #1010](https://github.com/arii/boomtick/issues/1010); merged in [PR #1013](https://github.com/arii/boomtick/pull/1013)).
- **Symptom:** `MCP error -32602: Invalid tools/call result: structuredContent: Invalid input: expected record, received array`.
- **Cause:** `listJulesSessionsHandler` (`mcp/src/tools/jules/list-sessions.ts`) returned `Promise<JulesSession[]>` — a bare array — while the declared `JulesListSessionsResponseSchema` is `z.object({ sessions: ... })`.
- **Fix:** Jules opened PR #1013 (`fix(mcp): wrap jules.list_sessions response in object`) with full unit test coverage. Merged into `main`.

### #1012 — Align resource URI schemes with MCP server discovery and add context status check tool
- **State:** RESOLVED / CLOSED ([Issue #1012](https://github.com/arii/boomtick/issues/1012); merged in [PR #1016](https://github.com/arii/boomtick/pull/1016)).
- **Symptom:** Small/medium LLMs calling `read_mcp_resource` deduce `server: "workspace"` and `server: "repo"` from URIs like `mcp://workspace/agent-contract` and `repo://package-json`, causing immediate MCP errors (`MCP server "workspace" has no resources. Servers with resources: boomtick`).
- **Fix:** Clarified server ownership in resource descriptors, added direct `repo_read_agent_contract` tool, added `repo_get_context_status` for `.agent-context.json` freshness inspection, and supported dynamic workspace resolution. Merged into `main`.

### Earlier Backlog & Proposed Features (from UX Evaluation Guide §2)
All items were filed and resolved in `arii/boomtick`:
- **Issue #992 — MCP typed schemas & resources:** Modernized `boomtick-mcp` with typed `outputSchema`, MCP resources interface, and streamable HTTP transport (merged in PR #998 / aggregate PR #1002).
- **Issue #993 — Review runner streamlining:** Added AST-aware diff truncation, lockfile filtering, and native GitHub ```` ```suggestion ```` blocks (merged in PR #995 / aggregate PR #1002).
- **Issue #994 — `td` offline workstation engine:** Solidified `td` in Python for offline Tree-sitter AST audits (`td audit`) and multi-PR conflict simulation (`td preflight`) (merged in PR #997 / aggregate PR #1002).
- **Release Please Component Versioning:** Configured in `release-please-config.json` for `cli`, `mcp`, and action components. Active via PR #1018.
- **Legacy `repo://agent-contract` URI removed:** Removed in definitions and server handlers; canonical URI is `mcp://workspace/agent-contract`.
- **v0.15 aggregate backlog:** Issues #986 (multi-repo context), #990 (MCP `create_branch` cwd bug), #983 (docs) closed via v0.15 aggregate PRs #1001 / #1002.
- **Issue #969 — Config loading outside repo:** Lazy config loading implemented and resolved in PR #975 (`fix(cli): load project config lazily to avoid crash outside repo`).

## Watch list
- Import-time config crash draft (from 2026-10-28 notes): never verified, never
  filed. Re-check if `td` cold-start failures appear in buttons logs.
