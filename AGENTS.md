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

## Current-version and failure contract (issue #1605)

- No legacy or backward-compatibility branches, deprecated aliases, or silent downgrade paths. Update callers to the current supported contract instead of preserving an old interface.
- Pin dependencies and owned Actions to the latest verified upstream main/release revision appropriate to that component. Record an immutable revision, not a floating `latest` reference. Review the change and run the affected checks before merging; do not keep a feature-branch pin after its main replacement exists.
- Failures must fail loudly: return a nonzero exit or an explicit failed result with the failing operation and cause. Never turn missing tools, failed execution or unavailable evidence into success, a warning-only pass, or invented output.
- We own the actions in our repositories. An obsolete pin or broken owned action is a correctness/maintenance defect, not automatically a security restriction. Diagnose and fix it. A security claim needs concrete evidence of an actual security boundary or threat; ownership does not waive secrets protection, access controls, user approval or spending limits.
- The platform-variance exception above does not permit support for retired versions or fallback chains that hide errors. If the current contract cannot run, report the failure rather than retrying older APIs.

## Working rules

Why: cruft from earlier agent sessions (generated assets, unused pins, duplicate helpers, docs for code that never existed) cost more to remove than to write. These rules keep it from coming back.

- Search for an existing helper before writing one; duplicates cost us later.
- Edit before you add. New files need a one-line reason in the PR.
- No new dependency without saying what it replaces or enables.
- No placeholder code that returns fake success. Fail loudly or don't ship.
- Run the smoke test (`bash scripts/check-format-diff.sh && npm run lint && npm test`, plus `npm run test:root`) before reporting done, and paste the result.
- Keep PRs small enough to read. Component status lives in [STATUS.md](STATUS.md); update it when a component's state changes.

## Commands

- `./scripts/buttons-up.sh` — restart the buttons service (buttons only; never bare `docker compose up -d` or the terminal service — that kills the shell you run in)
- `pnpm run lint` — run the lint task
- `bash scripts/check-format-diff.sh && npm run lint && npm test` — reproduce the CI gates locally (diff-aware ruff format check, shell + compile-check + ruff + tsc, then backend + frontend suites; backend needs `pip install -r backend/requirements.txt -r backend/requirements-test.txt` first, lint:python needs `pip install ruff`)
- `bash scripts/prune-worktrees.sh [--prune]` — sweep stale worktrees (report-only by default). After a PR merges, remove its worktree and delete the branch the same turn; never leave merged worktrees behind. The script only removes clean trees whose branch landed, never the main checkout, dirty trees, or unmerged work.

## Agent lanes

- Multi-session lane work is coordinated through memory file `lanes/registry.md` (personal_project scope): role → session-name roster. Read it to find the orchestrator/coders/auditors; never guess session names.
- Lane skills live in `.agents/skills/` (force-added; the directory is otherwise ignored). Imported/upstream skills stay ignored as re-fetchable.
- `.agents/skills/` is the single skill source of truth; `.claude/skills/` holds only symlinks into it (CI-guarded by `scripts/check-no-shadow-skills.sh`).

## Code Map

- `backend` — application source (incl. relocated tooling: `python3 -m backend.repair_worker …`, `python3 -m backend.impact_pass …`)
- `backend/shared` — modules shared by backend and deep-agent (`cli`, `runs`, `pricing`, `model_router`, `s5tools`, `ledger`, `twt_*`); import as `from backend.shared import <module>`, run entry points as `python3 -m backend.shared.<module>`
- `deep-agent` — LangGraph agents + evals (tracked files only; see `.gitignore` allowlist)
- `tests` — automated tests
- `docs` — project documentation
- `config` — project configuration

## Conventions

- Use `import` and `from ... import` for Python module imports.
- Name test files `test_*.py`.
