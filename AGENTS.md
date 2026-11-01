# Context: Working Directory & Repository Mapping

This machine runs the S5 remote server stack. You are running inside the terminal container where host checkouts are bind-mounted.

Always determine your target repository from your current working directory (`cwd`):
- `/srv/remote-server` (or subdirectories) → Repository is `arii/remote-server` (short name: `remote-server`).
- `/srv/repos/<name>` (or subdirectories) → Repository is `arii/<name>` (short name: `<name>`, e.g. `/srv/repos/tech-dancer` → `arii/tech-dancer`).
- If in another directory, check `git remote get-url origin` or `git rev-parse --show-toplevel`.

**Do not ask the user for the repository name (e.g. org/repo).** The current repository is always established by your working directory.

When using BoomTick MCP tools:
- The default MCP server configuration points to `arii/tech-dancer`.
- If you are in `/srv/remote-server` or another repository under `/srv/repos/`, provide the `repo` parameter override in `"owner/repo"` format (for example: `"repo": "arii/remote-server"`).
- Never pass placeholder string `"org/repo"` or null values to optional schema fields.
