---
name: boomtick
description: Instructions and schema reference for calling BoomTick MCP tools (workflow-runs, ci-logs, repo, and PR tools) without validation errors.
---

# BoomTick MCP Tools & Schemas

The BoomTick MCP server (`boomtick`) exposes tools for GitHub operations, repository inspections, Jules macro-agent sessions, and CI/workflow log inspections.

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

### `mcp__boomtick__repo_get_workflow_runs`
- **Description:** List recent GitHub Actions workflow runs for the repository.
- **Parameters:**
  - `repo` (string, optional): Target GitHub repository override (e.g. `"arii/remote-server"`).
  - `branch` (string, optional): Filter workflow runs by branch name (e.g. `"main"`). Omit if not filtering.
  - `event` (string, optional): Filter by event (e.g. `"push"`, `"pull_request"`). Omit if not filtering.
- **Correct Call Examples:**
  ```json
  {"repo": "arii/remote-server"}
  ```
  ```json
  {"repo": "arii/tech-dancer", "branch": "main"}
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

### `mcp__boomtick__repo_logs`
- **Description:** Read git commit or run logs for a PR.
- **Parameters:**
  - `prNumber` (integer, required)
  - `grep` (string, optional)
  - `repo` (string, optional)

### Common GitHub Tools
- `mcp__boomtick__github_search_open_prs`: accepts `repo` (optional string).
- `mcp__boomtick__github_get_pr`: accepts `prNumber` (required integer), `repo` (optional string).
- `mcp__boomtick__github_get_pr_diff`: accepts `prNumber` (required integer), `repo` (optional string).
- `mcp__boomtick__github_get_merge_conflict_files`: accepts `baseBranch`, `headRef`, `repo`.
