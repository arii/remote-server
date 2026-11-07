---
name: boomtick
description: Instructions and schema reference for calling BoomTick MCP tools (RAG, repo, GitHub, gates) without validation errors.
---

# BoomTick MCP Tools & Schemas

The BoomTick MCP server (`boomtick`) exposes tools for RAG init/query, repository inspections, GitHub operations, deterministic gates, and PR submission.

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

**Rule 1: Never pass explicit `null` for optional parameters.**
Passing `branch: null`, `event: null`, or `repo: null` causes Zod validation errors (`Invalid input: expected string, received null`). Always **omit** keys you have no value for.

**Rule 2: Only call tools on the live surface.** The server exposes
exactly the tools below (post `td_init_and_rag` split + worktree
removal). Anything else — `repo_get_workflow_runs`,
`github_search_open_prs`, `github_get_pr_diff`,
`github_get_merge_conflict_files`, `repo_get_changed_files`,
`td_init_and_rag`, `worktree_create/list/remove` — does not exist:
do not call it, do not document it back.

### RAG: init once, query separately
- `mcp__boomtick__td_init`: initialize a repo (fast-forward main,
  sync deps, index RAG). All parameters optional (`repoRoot` to
  override the git root).
- `mcp__boomtick__td_rag`: query the RAG index without rebuilding.
  `query` (string, required); `repoRoot`, `topK` optional.
  ```json
  {"query": "where is the attention queue assembled?"}
  ```

### `mcp__boomtick__repo_read_ci_logs`
- **Description:** Read CI logs for a given pull request.
- **Parameters:**
  - `prNumber` (integer, required): The pull request number.
  - `all` (boolean, optional): Include logs for successful runs (default `false`).
  - `repo` (string, optional): Target repository in `owner/repo` format.
- **Correct Call Example:**
  ```json
  {"prNumber": 42, "repo": "arii/tech-dancer"}
  ```

### GitHub Tools (all accept optional `repo` in `owner/repo` format)
- `mcp__boomtick__github_get_pr`: `prNumber` (required integer).
- `mcp__boomtick__github_issue_view`: `issueNumber` (required integer).
- `mcp__boomtick__github_issue_update`: `issueNumber` (required integer).
- `mcp__boomtick__github_issue_comment`: `issueNumber` (required integer).
- `mcp__boomtick__github_create_issue`: `title` (required string).

### Repo & Deterministic Gates
- `mcp__boomtick__repo_read_agent_context`: repository index and metadata.
- `mcp__boomtick__repo_run_tests`: run repository tests and checks.
- `mcp__boomtick__td_validate_issue`: validate a design spec file.
- `mcp__boomtick__td_verify_scope`: gate worktree changes against a spec.
- `mcp__boomtick__td_audit_worktree`: slop/AST audit with blast-radius analysis.
- `mcp__boomtick__agent_submit_pr`: submit a branch as a PR (draft by default).
- `mcp__boomtick__boomtick_health`: MCP server health and configuration.

Worktree lifecycle has no MCP tools — use the `td worktree`
CLI (`create` / `list` / `remove`) instead.
