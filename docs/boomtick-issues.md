# BoomTick-side issues (tracked from remote-server)

Separation of concerns: fixes in `arii/BoomTick` (MCP server `mcp/` + `td` CLI
`cli/`) cannot be pushed by the GitHub connector (403 on that repo — writes go
through Ari or his local agent). This file tracks them here so nothing is lost.
Verified against BoomTick `main` on 2026-10-29; states reconciled with live GitHub on 2026-11-02.

## Resolved (closed on GitHub — verified 2026-11-02)

### #1024 — `td preflight` runs BoomTick-repo checks on every repo
- **State:** RESOLVED / CLOSED ([Issue #1024](https://github.com/arii/boomtick/issues/1024); fixed via [PR #1028](https://github.com/arii/boomtick/pull/1028), merged 2026-10-29).
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

### #1025 — Clarify repo override param description, surface errors, and make branch/event optional with null-safety in `repo_get_workflow_runs` & CI-logs tools
- **State:** RESOLVED / CLOSED ([Issue #1025](https://github.com/arii/boomtick/issues/1025); closed 2026-11-01).
- **Symptom:**
  1. Passing `branch: null` or `event: null` to `repo.get_workflow_runs` (or other CI-logs / workflow tools) causes Zod validation errors: `Invalid input: expected string, received null`, surfaced to agents as `Failed to fetch workflow runs: Unknown error`.
  2. LLMs (e.g. Pi) literally pass `{"repo": "org/repo"}` and receive `Failed to fetch workflow runs: Unknown error`.
- **Cause:**
  1. Input schemas define `branch: z.string().optional()` and `event: z.string().optional()`. In Zod, `.optional()` does not allow `null` unless `.nullable()` is also specified (`z.string().nullable().optional()`). When LLMs serialize explicit nulls (`{ branch: null, event: null }`), validation throws `ZodError`.
  2. Schema description `(e.g. org/repo)` induces literal usage, and MCP handler error sanitization swallows GitHub 404 details into "Unknown error".
- **Fix:**
  1. Update `GetWorkflowRunsInputSchema` in `src/tools/repo.get_workflow_runs.ts` (and corresponding CI-logs / repo tool schemas):
     ```typescript
     export const GetWorkflowRunsInputSchema = z.object({
       branch: z.string().nullable().optional().describe("Filter workflow runs by branch name."),
       event: z.string().nullable().optional().describe("Filter workflow runs by event (e.g. push, pull_request)."),
       repo: z.string().nullable().optional().describe("Target GitHub repository override in owner/repo format (e.g. arii/tech-dancer). Defaults to current repository."),
     });
     ```
  2. Normalize inputs in handlers so `branch ?? undefined`, `event ?? undefined`, and `repo ?? undefined` are passed down cleanly, or treat null/empty strings as omitted filters.
  3. Ensure stderr details from `td repo workflow-runs` (such as 404 / repo not found) are preserved and surfaced cleanly instead of collapsing into `Unknown error`.

### #1026 — Sanitize dot-notated tool names to conform with MCP client tool naming regex
- **State:** RESOLVED / CLOSED ([Issue #1026](https://github.com/arii/boomtick/issues/1026); closed 2026-11-01).
- **Symptom:** Antigravity CLI and standard MCP clients reject 17 dot-notated tool registrations at startup (`violates ^[a-zA-Z0-9_-]{1,64}$`).
- **Fix:** Canonicalize tool registrations to underscores or sanitize manifests.

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

## Pending (audited 2026-11-02 — duplicate check against live GitHub state)

- P1 (boomtick preflight per-repo detection): NOT filed — duplicate of boomtick #1024, fixed via PR #1028 (merged 2026-10-29) and closed.
- P2 (boomtick null-safe inputs + surfaced errors): NOT filed — duplicate of boomtick #1025, closed 2026-11-01.
- Dot-notated tool names: NOT filed — duplicate of boomtick #1026, closed 2026-11-01.
- P3 (remote-server canonical repo path): FILED as [arii/remote-server#192](https://github.com/arii/remote-server/issues/192).

Note: reconciled 2026-11-02 — #1024–#1026 moved to Resolved; no open BoomTick-side items remain in this file.

## Filed 2026-11-02 (td-first gaps from S5 audit)

- [arii/boomtick#1082](https://github.com/arii/boomtick/issues/1082) — `td gh pr-view`: single PR state snapshot (state, reviews, checks, mergeable). Unblocks dropping raw `gh pr view` from `ask_investigator.pr_state` and briefing rollup.
- [arii/boomtick#1083](https://github.com/arii/boomtick/issues/1083) — `td gh run-jobs`: list jobs for a workflow run. Unblocks dropping raw `gh api repos/.../actions/runs/.../jobs` from briefing.
- Switched same day (td already covered): `fetch_pr_diff` now uses `td --json gh pr-diff` instead of `gh pr diff`. Deleted dead `deep-agent/pr_triage_agent.py` (399 lines, zero references; merge path already superseded by released td gate).
