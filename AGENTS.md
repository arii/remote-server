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

- `./scripts/buttons-up.sh` — restart the buttons service (buttons only; never bare `docker compose up -d` or the terminal service — that kills the shell you run in)
- `pnpm run lint` — run the lint task
- `npm run lint && npm test` — reproduce the CI gates locally (shell + compile-check + ruff + tsc, then backend + frontend suites; backend needs `pip install -r backend/requirements.txt` first, lint:python needs `pip install ruff`)

## Agent lanes

- Multi-session lane work is coordinated through memory file `lanes/registry.md` (personal_project scope): role → session-name roster. Read it to find the orchestrator/coders/auditors; never guess session names.
- Lane skills live in `.agents/skills/` (force-added; the directory is otherwise ignored). Imported/upstream skills stay ignored as re-fetchable.
- `.agents/skills/` is the single skill source of truth; `.claude/skills/` holds only symlinks into it (CI-guarded by `scripts/check-no-shadow-skills.sh`).

## Code Map

- `backend` — application source (incl. relocated tooling: `python3 -m backend.repair_worker …`, `python3 -m backend.impact_pass …`)
- `deep-agent` — LangGraph agents + evals (tracked files only; see `.gitignore` allowlist)
- `tests` — automated tests
- `docs` — project documentation
- `config` — project configuration

## Conventions

- Use `import` and `from ... import` for Python module imports.
- Name test files `test_*.py`.
