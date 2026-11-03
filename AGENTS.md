# Context: Working Directory & Repository Mapping

This machine runs the S5 remote server stack. You are running inside the terminal container where host checkouts are bind-mounted.

Always determine your target repository from your current working directory (`cwd`):
- `/srv/remote-server` (or subdirectories) → Repository is `arii/remote-server` (short name: `remote-server`).
- `/srv/repos/<name>` (or subdirectories) → Repository is `arii/<name>` (short name: `<name>`, e.g. `/srv/repos/boomtick` → `arii/boomtick`).
- If in another directory, check `git remote get-url origin` or `git rev-parse --show-toplevel`.

**Do not ask the user for the repository name (e.g. org/repo).** The current repository is always established by your working directory.

When using BoomTick MCP tools:
- The default MCP server configuration points to `arii/tech-dancer`.
- If you are in `/srv/remote-server` or another repository under `/srv/repos/`, provide the `repo` parameter override in `"owner/repo"` format (for example: `"repo": "arii/remote-server"`).
- Never pass placeholder string `"org/repo"` or null values to optional schema fields.

## Code discipline

- **Ban workaround chains**: Never write fallback chains that retry the same operation with tweaked flags/arguments to dodge a failure (e.g. `cmd || slightly-different cmd`, chained `try ... except` ladders papering over sequential errors, or fallback ladders where each rung papers over the previous failure).
- **Fix root causes**: Diagnose the actual failure, fix the root cause, and keep ONE canonical command or code path.
- **Allowed exception**: A fallback is permitted ONLY for genuine platform variance (e.g., Linux vs macOS) and MUST include an explanatory comment documenting why the platform variance exists.

## Commands

- `docker compose up -d --build` — start the project
- `pnpm run lint` — run the lint task

## Agent lanes

- Multi-session lane work is coordinated through memory file `lanes/registry.md` (personal_project scope): role → session-name roster. Read it to find the orchestrator/coders/auditors; never guess session names.
- Lane skills live in `.agents/skills/` (force-added; the directory is otherwise ignored). Imported/upstream skills stay ignored as re-fetchable.

## Code Map

- `backend` — application source
- `core_cli` — unified repo tooling CLI (`python3 -m core_cli …`)
- `tests` — automated tests
- `docs` — project documentation
- `config` — project configuration

## Conventions

- Use `import` and `from ... import` for Python module imports.
- Name test files `test_*.py`.
