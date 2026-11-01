# BoomTick-side issues (tracked from remote-server)

Separation of concerns: fixes in `arii/BoomTick` (MCP server `mcp/` + `td` CLI
`cli/`) cannot be pushed by the GitHub connector (403 on that repo — writes go
through Ari or his local agent). This file tracks them here so nothing is lost.
Verified against BoomTick `main` on 2026-10-29.

## Open

### #1024 — `td preflight` runs BoomTick-repo checks on every repo
- **State:** OPEN ([Issue #1024](https://github.com/arii/boomtick/issues/1024)).
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
- **State:** OPEN ([Issue #1025](https://github.com/arii/boomtick/issues/1025)).
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
- **State:** OPEN ([Issue #1026](https://github.com/arii/boomtick/issues/1026)).
- **Symptom:** Antigravity CLI and standard MCP clients reject 17 dot-notated tool registrations at startup (`violates ^[a-zA-Z0-9_-]{1,64}$`).
- **Fix:** Canonicalize tool registrations to underscores or sanitize manifests.

### #1027 — `td agent run-feedback-check` / `JulesFeedbackDaemon` fails on cross-repo sessions (GitHub 404) & >5 session batch search (Validation Failed)
- **State:** OPEN ([Issue #1027](https://github.com/arii/boomtick/issues/1027)).
- **Symptom:**
  Running `td --json agent run-feedback-check` in `/srv/repos/tech-dancer` (triggered by the S5 buttons `nudge-agents` action) crashes with exit code 1:
  ```
  INFO:dev_tools.daemon:Starting Jules Feedback Daemon
  INFO:dev_tools.daemon:Found 10 sessions
  INFO:dev_tools.daemon:Performing batch PR search for 10 session IDs
  WARNING:dev_tools.daemon:Batch PR search failed, will fallback to individual lookups: GitHub API Error: Validation Failed: [{"message": "More than five AND / OR / NOT operators were used.", "field": "q", "resource": "Search", "code": "invalid"}]
  ...
  INFO:dev_tools.daemon:Processing session 3095905648412883634
  {
    "status": "error",
    "message": "Unexpected error in agent run-feedback-check: GitHub API Error: Not Found",
    "code": 1
  }
  ```
- **Causes:**
  1. **Cross-repository PR lookup 404 (`cli/dev_tools/daemon.py` & `cli/dev_tools/orchestrator.py`):**
     - `JulesClient.list_sessions()` lists all user Jules sessions globally across all repositories (e.g. `arii/remote-server`, `arii/tech-dancer`).
     - In `Orchestrator.get_pr_for_session`, if a session has PR URL output (e.g., `https://github.com/arii/remote-server/pull/63`), it extracts only the numeric PR ID (`match.group(1)` -> `63`) without capturing the repository.
     - `JulesFeedbackDaemon._get_pr_for_session` and `Orchestrator.trigger_jules_feedback` call `self.github.fetch_pr_details(prNumber)`. Since `fetch_pr_details` defaults to `target_repo = repo or self.repo`, and `td` was run from `tech-dancer` (`self.repo == "arii/tech-dancer"`), it calls `GET /repos/arii/tech-dancer/pulls/63`.
     - PR #63 belongs to `arii/remote-server` and does not exist in `arii/tech-dancer`, returning HTTP 404 (`GitHub API Error: Not Found`).
     - Furthermore, `daemon._process_session` does not catch or handle exceptions during `_get_pr_for_session` or when processing foreign-repo sessions, causing unhandled exceptions to crash the daemon command.
  2. **GitHub Search API limit on boolean operators (`cli/dev_tools/daemon.py`):**
     - `_pre_match_sessions_batch` builds `id_query = " OR ".join([f'"{sid}"' for sid in session_ids])`.
     - When `len(session_ids) > 5` (default `limit=10` fetches 10 sessions), the query contains 9 `OR` operators. GitHub Search API strictly enforces a maximum of 5 boolean operators (`AND`, `OR`, `NOT`) per query, causing `GitHub API Error: Validation Failed: [{"message": "More than five AND / OR / NOT operators were used."}]`.
- **Fix:**
  1. In `cli/dev_tools/daemon.py`, chunk `session_ids` into batches of at most 5 IDs (e.g., `for chunk in [session_ids[i:i+5] for i in range(0, len(session_ids), 5)]`), and construct search queries with at most 4 `OR` operators each.
  2. In `cli/dev_tools/orchestrator.py`, update `get_pr_for_session(session)` to extract both the repository and PR number:
     ```python
     # Extract repo and PR from output URL: https://github.com/owner/repo/pull/123
     match = re.search(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", output["pullRequest"]["url"])
     ```
     Or filter sessions in `_process_session` so that only sessions belonging to the current `self.github.repo` (matching `sourceContext.source` or `outputs.pullRequest.url`) are processed by `fetch_pr_details` and `trigger_jules_feedback`.
  3. In `cli/dev_tools/daemon.py`, wrap `_get_pr_for_session` and `trigger_jules_feedback` per-session lookups in try/except blocks, logging a warning and skipping foreign or deleted PRs rather than aborting the entire feedback check.

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
