# Global Repository & Harness Context for Pi

You are running inside the S5 terminal container. Repositories are located at:
- `/srv/remote-server`: The infrastructure, terminal, buttons, and deployment repo (`arii/remote-server`).
- `/srv/repos/<name>`: Checked out project repositories (e.g. `/srv/repos/tech-dancer` → `arii/tech-dancer`).

## Repository Resolution Rules
1. Automatically determine repository context from `process.cwd()` / current working directory.
   - If cwd is within `/srv/remote-server` → repo is `arii/remote-server`.
   - If cwd is within `/srv/repos/<repo>` → repo is `arii/<repo>`.
2. Do not prompt the user to specify or confirm the repository name or `org/repo`.
3. When using BoomTick MCP tools (`repo.*`, `github.*`):
   - By default, the MCP server environment is scoped to `tech-dancer`.
   - To target the current repository or a different repo, supply the `repo` parameter override in `"owner/repo"` format: e.g. `{"repo": "arii/remote-server"}`.
   - Never supply explicit `null` for optional parameters (`branch`, `event`, `repo`). Simply omit keys that do not have a concrete value.
