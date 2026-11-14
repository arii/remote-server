---
name: boomtick
description: Instructions and schema reference for calling BoomTick MCP tools (RAG, repo, GitHub, gates) without validation errors.
---

# BoomTick MCP Tools & Schemas

The BoomTick MCP server (`boomtick`) exposes 12 tools for RAG init/query,
repository context/tests, GitHub operations, deterministic gates, PR
submission, and workflow runs. Mode-heavy tools use explicit mode fields (`include`, `search`,
write fields) — never optional-field telegraphy.

## Working Directory & Repo-Override Convention

1. **Auto-derive Repo from CWD:**
   - When running in `/srv/remote-server` (or subdirectories), your repo is `arii/remote-server`.
   - When running in `/srv/repos/<name>` (or subdirectories), your repo is `arii/<name>`.
   - **Never prompt the user to ask for the repo name or `org/repo`.**

2. **The `repo` Override Parameter:**
   - The MCP server's default configuration in `mcp.json` targets `arii/tech-dancer`.
   - Whenever you operate on a different repo (e.g. `/srv/remote-server`), pass the `repo` parameter in `owner/repo` format (e.g. `"repo": "arii/remote-server"`).
   - **Never pass literal `"org/repo"`**.
   - If operating on the default repository (`tech-dancer`), you can omit `repo` or pass `"repo": "arii/tech-dancer"`.

## Tool Schemas & Parameter Discipline

**Rule 1: Optional parameters are omission-only.** Pass only the keys you
have a value for; schemas name fields exactly (no `branch`/`branchName`
aliases) and unknown or mistyped fields surface as validation errors.

**Rule 2: Only call tools on the live surface.** The server exposes
exactly the 12 tools below (post the 2026-11 surface consolidation
plus the `run_workflow` gateway).
Anything else — the retired names `boomtick_health`,
`repo_read_agent_context`, `repo_read_ci_logs`, `repo_run_tests`,
`github_get_pr`, `github_issue_view`, `github_issue_update`,
`github_issue_comment`, plus the long-dead `repo_get_workflow_runs`,
`github_search_open_prs`, `github_get_pr_diff`,
`github_get_merge_conflict_files`, `repo_get_changed_files`,
`td_init_and_rag`, `worktree_create/list/remove` — returns
"Tool not found": do not call it, do not document it back.

### Context & health: `repo_context`
- `mcp__boomtick__repo_context`: repository index/metadata, ecosystem
  status, and server health in one tool.
  - `repo` (string, optional): `owner/repo` override.
  - `scope` (enum `"self"|"all"`, optional): omit or `"self"` reads one
    repo; `"all"` fetches ecosystem-wide context.
  - `deep` (boolean, optional): include server health details (td
    availability, RAG index state). Replaces the old `boomtick_health`.
  - `skill` (string, optional): include a skill's SKILL.md body.
  ```json
  {"deep": true}
  ```

### Tests: `repo_test`
- `mcp__boomtick__repo_test`: run repository tests and checks (default
  commands: install, lint, test, build). Optional `commands` (array),
  `timeoutSeconds` (int, default 300), `worktreePath`.
  ```json
  {}
  ```

### RAG: init once, query separately
- `mcp__boomtick__td_init`: initialize a repo (fast-forward main,
  sync deps, index RAG). All parameters optional (`repoRoot` to
  override the git root).
- `mcp__boomtick__td_rag`: query the RAG index without rebuilding.
  `query` (string, required); `repoRoot`, `topK` optional.
  ```json
  {"query": "where is the attention queue assembled?"}
  ```

### `mcp__boomtick__github_pr` (view + search, modes are explicit)
- View one PR: pass `prNumber` (required integer) with optional `repo`.
  `include` (enum array) selects extra sections:
  `["diff", "reviews", "commits", "conflicts", "ciLogs", "workflowRuns"]`.
  - CI logs for a PR: `include: ["ciLogs"]` (replaces the old
    `repo_read_ci_logs`; run-level filters by branch/event were dropped —
    use the `td repo ci-logs` CLI for those).
  - `include: ["workflowRuns"]` lists workflow runs without needing a PR number.
- Search PRs: pass `search` (object) instead of `prNumber` —
  `{"state": "open"|"closed"|"all", "labels": ["..."], "limit": 100}`.
  Defaults: state `open`, no labels, limit 100.
- The two modes are mutually exclusive; a call without either errors.
  ```json
  {"prNumber": 42, "include": ["ciLogs"]}
  ```
  ```json
  {"prNumber": 42, "include": ["diff", "reviews", "conflicts"]}
  ```

### `mcp__boomtick__github_issue` (read + one write per call)
- `issueNumber` (required integer); `repo` optional.
- Omitting every write field reads the issue.
- Write fields (exactly one per call): `comment` (string, min length 1),
  `setBody` (string), `addLabels` / `removeLabels` (string arrays),
  `state` (`"open"|"closed"`). `comment` is exclusive with the other
  write fields — split a comment plus a state change into two calls.
  ```json
  {"issueNumber": 7, "comment": "Triage summary: CI failing on unit tests."}
  ```
  ```json
  {"issueNumber": 7, "state": "closed"}
  ```

### `mcp__boomtick__github_create_issue`
- `title` (required string, min length 1); `body` (string, optional,
  passed directly — the old `file` parameter is gone); `repo` optional.

### Deterministic gates & PR submission
- `mcp__boomtick__td_validate_issue`: validate a design spec file
  (`file` required; `repoRoot` optional).
- `mcp__boomtick__td_verify_scope`: gate worktree changes against a spec
  (`specFile`, `worktree` required; `autoRevert` defaults false).
- `mcp__boomtick__td_audit_worktree`: slop/AST audit with blast-radius
  analysis (`worktree` required; `includeImpact`, `topK` optional).
- `mcp__boomtick__agent_submit_pr`: submit a branch as a PR (draft by
  default; `pushMode: true` required to actually push).

### Workflows: `mcp__boomtick__run_workflow`
- Runs a constrained, fixed-graph workflow from the workflows registry;
  the request is validated deterministically and the response is a
  receipt with per-provider attempt traces.
- `workflow` (required string) names a registered workflow;
  `input` must match that workflow's own JSON schema — the per-workflow
  schemas live in the `mcp://workflows/registry` resource, not here.

Worktree lifecycle has no MCP tools — use the `td worktree`
CLI (`create` / `list` / `remove`) instead.
