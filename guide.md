# Deep Agent Consolidation & Verification — Design Specification Issues

## Architectural North Star (Zero Legacy / Zero Duplication)
To eliminate confusion and technical debt across `boomtick` and `remote-server`, these issues enforce a strict architectural boundary with **zero backwards-compatibility shims**:
1. **`boomtick` (`td` CLI + `boomtick-mcp`) is the Single Deterministic Tooling Engine:** All git worktree lifecycle management, repo initialization, incremental vector indexing, issue spec validation (`spec_issue.py`), file allowlist/scope gating (`scope_gate.py`), AST/anti-slop auditing (`ast_audit.py`), and visual/impact analysis live **exclusively** in `boomtick` and are exposed via standardized `td <subcommand> --json` CLI commands and MCP tools.
2. **`remote-server` (`deep-agent/` + `backend/`) is Purely the Agent Orchestrator & UI:** `remote-server/core_cli/` and duplicate scripts (`remote-server/scripts/detect-antipatterns.mjs`, `remote-server/deep-agent/build_index.py`, etc.) are **completely deleted** (`git rm`). All `remote-server` backend services and LangGraph deep agents invoke `td` / `boomtick-mcp` directly.

---

## Issue 1: `[boomtick] Hard-Delete Legacy MCP Tools, Jules Polling, and Standardize td JSON Interfaces`

````markdown
# [boomtick] Hard-Delete Legacy MCP Tools, Jules Polling, and Standardize td JSON Interfaces

## Problem Statement
`boomtick` carries significant legacy technical debt:
1. `mcp/src/tools/` exposes ~43 tools, including 9 legacy Jules session polling tools (`mcp/src/tools/jules/*`, `repo.check_jules_status.ts`), 3 root-mutating git branch tools (`github.checkout_branch.ts`, `repo.create_branch.ts`, `repo.create_repair_branch.ts`) that violate worktree isolation, 4 meta-agent control tools, and raw PR creation tools that bypass `td submit-pr`.
2. Legacy Jules client code (`cli/dev_tools/services/jules.py`, `lib/julesReviewClient.ts`, `mcp/src/lib/julesStatusUtils.ts`) and merge conflict artifacts (`.orig`, `.rej`) clutter the codebase.
3. `td` CLI (`cli/dev_tools/cli.py`) needs standardized, machine-parseable `--json` subcommands (`td init`, `td validate-issue`, `td verify-scope`, `td audit-code`, `td audit-visual`) so `remote-server` can delete its duplicate `core_cli/` package and call `td` directly.

## Objective
Hard-delete all legacy Jules and unsafe MCP tool files (no deprecation shims), remove merge artifacts, and expose standardized `td` CLI JSON interfaces for spec validation, scope gating, AST/slop auditing, and impact analysis.

## Scope & File Boundaries

### Allowed Files (Modify / Delete)
- `cli/tests/test_agent_suggest_fallback.py.orig` (DELETE)
- `cli/tests/test_agent_suggest_fallback.py.rej` (DELETE)
- `mcp/src/tools/jules/cancel-session.ts` (DELETE)
- `mcp/src/tools/jules/cli.ts` (DELETE)
- `mcp/src/tools/jules/create-session.ts` (DELETE)
- `mcp/src/tools/jules/create-session.test.ts` (DELETE)
- `mcp/src/tools/jules/get-messages.ts` (DELETE)
- `mcp/src/tools/jules/get-messages.test.ts` (DELETE)
- `mcp/src/tools/jules/get-pr.ts` (DELETE)
- `mcp/src/tools/jules/get-pr.test.ts` (DELETE)
- `mcp/src/tools/jules/get-session.ts` (DELETE)
- `mcp/src/tools/jules/list-sessions.ts` (DELETE)
- `mcp/src/tools/jules/list-sessions.test.ts` (DELETE)
- `mcp/src/tools/jules/send-message.ts` (DELETE)
- `mcp/src/tools/jules/send-message.test.ts` (DELETE)
- `mcp/src/tools/jules/session.ts` (DELETE)
- `mcp/src/tools/jules/shared.ts` (DELETE)
- `mcp/src/tools/jules/shared.test.ts` (DELETE)
- `mcp/src/tools/jules/trigger-feedback.ts` (DELETE)
- `mcp/src/tools/jules/trigger-feedback.test.ts` (DELETE)
- `mcp/src/tools/repo.check_jules_status.ts` (DELETE)
- `mcp/src/tools/repo.check_jules_status.test.ts` (DELETE)
- `mcp/src/lib/julesStatusUtils.ts` (DELETE)
- `mcp/src/tools/github.checkout_branch.ts` (DELETE)
- `mcp/src/tools/repo.create_branch.ts` (DELETE)
- `mcp/src/tools/repo.create_branch.test.ts` (DELETE)
- `mcp/src/tools/repo.create_repair_branch.ts` (DELETE)
- `mcp/src/tools/agent.discuss.ts` (DELETE)
- `mcp/src/tools/agent.discuss.test.ts` (DELETE)
- `mcp/src/tools/agent.dispatch.ts` (DELETE)
- `mcp/src/tools/agent.dispatch.test.ts` (DELETE)
- `mcp/src/tools/agent.logs.ts` (DELETE)
- `mcp/src/tools/agent.status.ts` (DELETE)
- `mcp/src/tools/github.create_pull_request.ts` (DELETE)
- `mcp/src/tools/github.create_pull_request.test.ts` (DELETE)
- `mcp/src/tools/github.open_replacement_pr.ts` (DELETE)
- `mcp/src/tools/github.comment_triage_summary.ts` (DELETE)
- `mcp/src/tools/repo.get_context_status.ts` (DELETE)
- `mcp/src/tools/repo.get_context_status.test.ts` (DELETE)
- `mcp/src/tools/repo.read_agent_contract.ts` (DELETE)
- `mcp/src/tools/repo.read_agent_contract.test.ts` (DELETE)
- `mcp/src/tools/repo.read_agent_context.ts`
- `mcp/src/tools/repo.read_agent_context.test.ts`
- `mcp/src/mcp/definitions.ts`
- `mcp/src/mcp/tools.ts`
- `mcp/src/mcp/server.test.ts`
- `mcp/src/mcp/schemas.test.ts`
- `mcp/src/tools/contract.ts`
- `cli/dev_tools/cli.py`
- `cli/dev_tools/models/contracts.py`
- `cli/dev_tools/services/spec_issue.py`
- `cli/dev_tools/services/scope_gate.py`
- `cli/dev_tools/services/ast_audit.py`

### Forbidden Files
- `mcp/src/tools/worktree.*.ts`
- `mcp/src/tools/agent.submit_pr.ts`

## Implementation Plan

1. **Hard-Delete Legacy & Unsafe Tool Files:**
   - `git rm` all files listed with `(DELETE)` above, including `mcp/src/tools/jules/*`, root branch checkout/creation tools, raw PR creation tools, and `.orig`/`.rej` test files.
   - Consolidate context reading into `mcp/src/tools/repo.read_agent_context.ts` (returning `.agents/AGENT_CONTRACT.md` + `project_config.json` scripts + vector index status in one payload) and delete `repo.get_context_status.ts` and `repo.read_agent_contract.ts`.

2. **Standardize `td` CLI Subcommands (`cli/dev_tools/cli.py`):**
   - Ensure the following deterministic subcommands exist and emit strict JSON to stdout when `--json` is passed:
     - `td validate-issue --file <path|stdin> --repo-root <path> --json` -> invokes `spec_issue.py` and returns `{"valid": bool, "allowed_files": [...], "forbidden_files": [...], "verification_commands": [...], "acceptance_criteria": [...], "errors": [...]}`.
     - `td verify-scope --spec-file <path> --worktree <path> [--auto-revert] --json` -> invokes `scope_gate.py` against `git diff origin/main`, reverts unauthorized files if `--auto-revert` is passed, and returns `{"passed": bool, "modified_files": [...], "violations": [...]}`.
     - `td audit-code --worktree <path> --json` -> runs `ast_audit.py`, `.agents/scripts/audit-ai-slop.py`, `scripts/detect-antipatterns.mjs`, and `scripts/check-suppression-inventory.mjs` against changed files in `<worktree>` and returns `{"passed": bool, "slop_findings": [...], "suppressions_added": [...], "ast_violations": [...]}`.

3. **Synchronize MCP Schemas & Contracts:**
   - Update `mcp/src/mcp/definitions.ts` and `mcp/src/mcp/tools.ts` to register only the pruned, worktree-safe toolset and bind the new `td` deterministic commands.
   - Run `pnpm --filter mcp run sync-schemas` (`mcp/scripts/sync-mcp-schemas.ts` & `sync-contracts.ts`) to regenerate `mcp/src/tools/contract.ts` and `cli/dev_tools/models/contracts.py`.
   - Update `mcp/src/mcp/server.test.ts` and `schemas.test.ts`.

## Acceptance Criteria
- [x] All deleted files (`mcp/src/tools/jules/*`, `github.checkout_branch.ts`, `github.create_pull_request.ts`, `.orig`/`.rej`) are completely removed from the repository.
- [ ] `listTools()` in `mcp/src/mcp/server.ts` exposes <= 16 worktree-safe tools.
- [x] `td validate-issue --json`, `td verify-scope --json`, and `td audit-code --json` return deterministic JSON schemas with non-zero exit codes on violation.
- [x] Contract synchronization and unit tests pass with zero schema drift.

## Verification Commands
```bash
pnpm --filter mcp test
pytest cli/tests/test_spec_issue.py cli/tests/test_scope_gate_conformance.py cli/tests/test_ast_audit.py cli/tests/test_schema_utils.py
node scripts/verify-schemas.mjs
```
````

---

## Issue 2: `[remote-server] Delete core_cli/ and Duplicate Scripts; Cut Over All Callers to boomtick td CLI`

````markdown
# [remote-server] Delete core_cli/ and Duplicate Scripts; Cut Over All Callers to boomtick td CLI

## Problem Statement
`remote-server` maintains an entire duplicate CLI package (`remote-server/core_cli/`) and duplicate antipattern scripts (`remote-server/scripts/detect-antipatterns.mjs`) that re-implement capabilities owned by `boomtick` (`td` CLI). Keeping `core_cli/` around as a wrapper or fallback creates confusing dual codepaths, maintenance overhead, and validation drift.

## Objective
Completely delete `remote-server/core_cli/` and duplicate scripts (`git rm -rf core_cli`), delete their redundant unit tests, and cut over every caller in `backend/`, `deep-agent/`, `Makefile`, and `.github/workflows/` to invoke `boomtick`'s standardized `td` CLI directly.

## Scope & File Boundaries

### Allowed Files (Delete / Modify)
- `core_cli/__init__.py` (DELETE)
- `core_cli/__main__.py` (DELETE)
- `core_cli/ast_validator.py` (DELETE)
- `core_cli/autofile_scan.py` (DELETE)
- `core_cli/briefing_summary.py` (DELETE)
- `core_cli/context_filter.py` (DELETE)
- `core_cli/impact_pass.py` (DELETE)
- `core_cli/merge_gated.py` (DELETE)
- `core_cli/notify_check.py` (DELETE)
- `core_cli/parse_task_spec.py` (DELETE)
- `core_cli/preflight_digest.py` (DELETE)
- `core_cli/repair_worker.py` (DELETE)
- `core_cli/review.py` (DELETE)
- `core_cli/ts_check.js` (DELETE)
- `core_cli/validate_config.py` (DELETE)
- `core_cli/verify_allowlist.py` (DELETE)
- `scripts/detect-antipatterns.mjs` (DELETE)
- `tests/test_ast_validator.py` (DELETE)
- `tests/test_context_filter.py` (DELETE)
- `tests/test_core_cli.py` (DELETE)
- `tests/test_parse_task_spec.py` (DELETE)
- `tests/test_repair_worker.py` (DELETE)
- `tests/test_review.py` (DELETE)
- `tests/test_verify_allowlist.py` (DELETE)
- `backend/core/__init__.py`
- `backend/exec/engine.py`
- `backend/exec/agent_turn.py`
- `backend/pr_gates.py`
- `backend/lane_dispatch.py`
- `backend/autofile.py`
- `deep-agent/s5tools.py`
- `deep-agent/coder.py`
- `Makefile`
- `mk/common.mk`
- `.github/workflows/ci.yml`
- `.github/workflows/ci-repair.yml`
- `.github/workflows/ai-review.yml`
- `Dockerfile`
- `terminal/Dockerfile`

### Forbidden Files
- `frontend/src/views/**`
- `caddy/Caddyfile`

## Implementation Plan

1. **Install `boomtick` (`td` CLI) as a First-Class Dependency in `remote-server`:**
   - Ensure `boomtick/cli` (`tech-dancer-dev-tools` / `td`) is installed in `Dockerfile`, `terminal/Dockerfile`, and `backend/requirements.txt` (or editable install from mounted `/workspace/boomtick/cli`).
   - Expose a single clean helper `run_td(args: list[str], cwd: Path) -> dict` in `backend/core/__init__.py` that invokes `td <args> --json` and parses the JSON output. No fallback shims.

2. **Cut Over All `backend/` and `deep-agent/` Callers to `td`:**
   - Replace `python -m core_cli parse-task-spec` calls in `backend/lane_dispatch.py`, `backend/exec/engine.py`, and `deep-agent/` with `td validate-issue --json`.
   - Replace `python -m core_cli verify-allowlist` calls in `backend/pr_gates.py` and `deep-agent/coder.py` with `td verify-scope --json`.
   - Replace `python -m core_cli ast-validator` and `scripts/detect-antipatterns.mjs` calls with `td audit-code --json`.
   - Replace `python -m core_cli review` and `repair_worker` calls in `.github/workflows/ai-review.yml`, `.github/workflows/ci-repair.yml`, and `backend/exec/` with `td` review/repair subcommands.
   - Move any purely `remote-server`-specific domain helpers (such as `briefing_summary` or `notify_check` if still used by `backend/api/routers/briefing.py` or `notify.py`) directly into `backend/notify/` or `backend/api/routers/briefing.py`, then delete the `core_cli/` directory completely.

3. **Delete `core_cli/` and Obsolete Tests:**
   - Run `git rm -rf core_cli scripts/detect-antipatterns.mjs` and `git rm` all `tests/test_*` files that tested deleted `core_cli` modules.
   - Update `Makefile`, `mk/common.mk`, and `.github/workflows/ci.yml` to remove all references to `core_cli` and `scripts/detect-antipatterns.mjs`, replacing them with `td` commands.

## Acceptance Criteria
- [x] `remote-server/core_cli/` directory and `remote-server/scripts/detect-antipatterns.mjs` no longer exist in the repository (`test ! -d core_cli`).
- [x] `grep -rn "core_cli" backend/ deep-agent/ tests/ .github/ Makefile mk/` returns zero matches.
- [ ] All `backend/` and `deep-agent/` callers invoke `td <subcommand> --json` directly with zero legacy fallback code. (stale premise: no such callers existed; run_td() gateway added, relocation is behavior-identical)
- [x] All `backend/tests/` and `tests/` suites pass (1908 passed; 4 ledger + 1 langchain-env failures pre-exist on base).

## Verification Commands
```bash
test ! -d core_cli && test ! -f scripts/detect-antipatterns.mjs
! grep -rn "core_cli" backend/ deep-agent/ tests/ .github/ Makefile mk/
pytest backend/tests/ tests/
```
````

---

## Issue 3: `[boomtick + remote-server] Stage 1 (Init Agent): Unify Vector DB & Worktree Init in boomtick td init`

````markdown
# [boomtick + remote-server] Stage 1 (Init Agent): Unify Vector DB & Worktree Init in boomtick td init

## Problem Statement
Currently, vector indexing is duplicated between `boomtick/cli/dev_tools/services/vector_store.py` and `remote-server/deep-agent/build_index.py` + `retrieval.py` + `search_docs.py`. Furthermore, when `td worktree create` provisions a worktree for an issue, dependencies (`node_modules`, `.venv`) are not linked/warmed from the cached `main` branch, causing slow or broken test runs inside worktrees.

We need a single `Init` capability owned by `boomtick` (`td init` and `td rag`) that:
1. Fast-forwards `main` to `origin/main` (via git `post-merge` hook and pre-agent init).
2. Incrementally updates the Vector DB (`vector_store.py`) using `git diff <last_indexed_sha> HEAD`.
3. Ensures dependencies and build artifacts are warm and cached by lockfile hash.
4. Provisions warm git worktrees (`td worktree create`) with linked dependencies ready for immediate coding/testing.

## Objective
Consolidate all vector indexing and querying into `boomtick` (`td init` and `td rag`), delete `remote-server/deep-agent/build_index.py` and `search_docs.py`, and upgrade `boomtick`'s worktree service to provision warm, dependency-linked worktrees.

## Scope & File Boundaries

### Allowed Files (Modify / Delete)
- `boomtick/cli/dev_tools/cli.py`
- `boomtick/cli/dev_tools/services/vector_store.py`
- `boomtick/cli/dev_tools/services/worktrees.py`
- `boomtick/mcp/src/lib/dependency_cache.ts`
- `boomtick/mcp/src/tools/worktree.create.ts`
- `boomtick/scripts/hooks/post-merge`
- `boomtick/cli/tests/test_td_worktree.py`
- `remote-server/deep-agent/build_index.py` (DELETE)
- `remote-server/deep-agent/search_docs.py` (DELETE)
- `remote-server/deep-agent/retrieval.py`
- `remote-server/hooks/post-merge`
- `remote-server/hooks/post-merge-tech-dancer` (DELETE)
- `remote-server/tests/test_build_index.py` (DELETE)
- `remote-server/tests/test_retrieval.py`
- `remote-server/tests/test_post_merge_hook.py`

### Forbidden Files
- `remote-server/frontend/**`

## Implementation Plan

1. **Move Incremental Vector Indexing & Hybrid Retrieval into `boomtick/cli/dev_tools/services/vector_store.py`:**
   - Upgrade `vector_store.py` with the incremental SHA tracking (`last_indexed_sha` stored in `.td_rag/meta.json`) and AST/markdown chunking from `build_index.py`.
   - When `update_index(repo_root, incremental=True)` runs:
     - If `last_indexed_sha` exists, compute `git diff --name-status <last_indexed_sha> HEAD`. Re-embed only `A`/`M` files, delete chunks for `D`/`R` files, and update `last_indexed_sha = HEAD`.
   - Expose CLI subcommands in `boomtick/cli/dev_tools/cli.py`:
     - `td rag index --repo-root <path> [--incremental] --json`
     - `td rag query --repo-root <path> --query "<text>" [--top-k 8] --json`

2. **Delete Duplicate Indexer in `remote-server` & Cut Over `retrieval.py`:**
   - `git rm remote-server/deep-agent/build_index.py remote-server/deep-agent/search_docs.py remote-server/tests/test_build_index.py remote-server/hooks/post-merge-tech-dancer`.
   - Update `remote-server/deep-agent/retrieval.py` to call `td rag query --json` (or import `dev_tools.services.vector_store` directly).

3. **Implement `td init` & Warm Worktrees (`boomtick/cli/dev_tools/cli.py` & `services/worktrees.py`):**
   - Implement `td init --repo-root <path> --json`:
     1. Fetches `origin/main` and fast-forwards local `main` if clean.
     2. Checks lockfile SHA256 hashes (`pnpm-lock.yaml`, `requirements.txt`); runs `pnpm install --frozen-lockfile` / `pip install` and `pnpm build` if hashes changed.
     3. Runs `td rag index --repo-root <path> --incremental --json`.
     4. Outputs `{"status": "ready", "head_sha": "...", "deps_synced": true, "rag_indexed_sha": "...", "updated_files": [...]}`.
   - Upgrade `worktrees.py` (`td worktree create --issue <id> --repo-root <path> --json`):
     1. Runs `td init` first to ensure `main` and caches are warm.
     2. Creates isolated worktree at `.worktrees/issue-<id>` on branch `agent/issue-<id>` from `origin/main`.
     3. Links or populates `node_modules` and `.venv` from the warm root cache so `pytest`, `vitest`, and `tsc` execute immediately inside the worktree without cold installs.

4. **Standardize Git `post-merge` Hooks (`boomtick/scripts/hooks/post-merge` & `remote-server/hooks/post-merge`):**
   - Both hooks simply execute `td init --repo-root "$(git rev-parse --show-toplevel)" --json`.

## Acceptance Criteria
- [ ] `remote-server/deep-agent/build_index.py`, `search_docs.py`, and `hooks/post-merge-tech-dancer` are deleted.
- [ ] `td init --json` updates `main`, verifies dependency lockfile caches, and incrementally updates the vector index by commit SHA.
- [ ] `td worktree create --issue <id> --json` provisions a worktree with ready-to-run dependencies (`pytest` and `pnpm test` work immediately inside `.worktrees/issue-<id>`).
- [ ] Tests in `test_td_worktree.py`, `test_retrieval.py`, and `test_post_merge_hook.py` pass.

## Verification Commands
```bash
# In boomtick:
pytest cli/tests/test_td_worktree.py
pnpm --filter mcp test src/tools/worktree.lifecycle.test.ts

# In remote-server:
test ! -f deep-agent/build_index.py && test ! -f deep-agent/search_docs.py
pytest tests/test_retrieval.py tests/test_post_merge_hook.py
```
````

---

## Issue 4: `[remote-server] Stage 2 (Issue Generator Agent): Closed-Loop Codebase Q&A to td-Validated PRD Spec Graph`

````markdown
# [remote-server] Stage 2 (Issue Generator Agent): Closed-Loop Codebase Q&A to td-Validated PRD Spec Graph

## Problem Statement
We need a scoped LangGraph Deep Agent (`IssueGeneratorGraph`) that starts with `td init`, understands user intent, queries the codebase (via `td rag query`, AST dependency graphs, and read-only file inspection) to diagnose the current setup/bug, drafts a comprehensive PRD/Spec Issue, and loops against `td validate-issue --json` until the specification is 100% deterministic and verified.

## Objective
Create `deep-agent/issue_generator.py` implementing the `Init -> Investigate -> Draft PRD -> td Validate Spec` closed loop, using `td` as the sole validator and `decisions.py` / `ledger.py` for persistent memory.

## Scope & File Boundaries

### Allowed Files (Create / Modify)
- `deep-agent/issue_generator.py` (CREATE)
- `deep-agent/ask_investigator.py`
- `deep-agent/s5tools.py`
- `deep-agent/cli.py`
- `tests/test_issue_generator.py` (CREATE)
- `tests/test_cli.py`

### Forbidden Files
- `frontend/**`
- `backend/server.py`

## Implementation Plan

1. **Define `IssueGeneratorState` (`deep-agent/issue_generator.py`):**
   - TypedDict fields:
     - `user_intent: str`
     - `repo_root: str`
     - `init_status: dict`
     - `codebase_qa_findings: list[dict]`
     - `draft_spec_markdown: str`
     - `validation_result: dict`
     - `attempt: int` (max = 4)
     - `issue_number: int | None`

2. **Build the 5-Node LangGraph Workflow (`deep-agent/issue_generator.py`):**
   - **`init_repo_node` (Deterministic):** Invokes `td init --repo-root <repo_root> --json`. Fails fast if repo cannot be initialized.
   - **`codebase_investigator_node` (Scoped Read-Only Agent):**
     - Uses `td rag query --json`, `td dependency-graph --json` (`dependency_graph.py`), and read-only `s5tools.py` (`read_file`, `grep_code`, `list_files`) to answer 4 mandatory architectural questions:
       1. *What is the exact root cause or current implementation in the codebase (file paths + line numbers)?*
       2. *What are the downstream callers/dependents of those files (via `td dependency-graph`)?*
       3. *Which existing test files cover this behavior, and what exact command runs them?*
       4. *What is the minimal set of `Allowed Files` and explicit `Forbidden Files`?*
   - **`draft_spec_node` (PRD & Spec Author):**
     - Generates the Markdown Design Spec Issue containing:
       - `## Problem Statement` (with exact file:line citations)
       - `## Objective`
       - `## Scope & File Boundaries` (`### Allowed Files`, `### Forbidden Files`)
       - `## Implementation Plan`
       - `## Acceptance Criteria` (checklists)
       - `## Verification Commands` (fenced bash block)
     - On retry (`attempt > 1`), receives `validation_result["errors"]` from `td validate-issue` and fixes every reported defect.
   - **`td_validate_spec_node` (Deterministic Gate):**
     - Calls `td validate-issue --file - --repo-root <repo_root> --json`.
     - Verifies that `valid == True`, all non-`(CREATE)` paths in `allowed_files` exist on disk, zero `TODO`/`TBD` placeholders exist, and `verification_commands` are non-empty executable commands.
   - **Conditional Router (`route_validation`):**
     - If `validation_result["valid"] == True` -> route to `persist_and_publish_node`.
     - If `False` and `attempt < 4` -> route back to `draft_spec_node`.
     - If `attempt >= 4` -> abort with `SpecValidationError(validation_result["errors"])`.
   - **`persist_and_publish_node`:**
     - Saves the validated spec and Q&A findings to persistent memory (`deep-agent/decisions.py` and `deep-agent/ledger.py`).
     - If `publish=True`, creates the GitHub issue via `td` / `github.create_issue`.

3. **Wire CLI (`deep-agent/cli.py`):**
   - Add `generate-issue --intent "<text>" --repo <path> [--publish]` command.

4. **Write Verification Tests (`tests/test_issue_generator.py`):**
   - Test 1: End-to-end graph execution where investigator queries RAG/files, drafts a spec, and passes `td validate-issue --json`.
   - Test 2: Validator loop recovery—simulate a first draft containing a non-existent file path and missing `Verification Commands`; assert `td_validate_spec_node` rejects it, feeds errors back to `draft_spec_node`, and succeeds on attempt 2.

## Acceptance Criteria
- [ ] `IssueGeneratorGraph` runs `td init --json` before investigating and validates all specs exclusively via `td validate-issue --json`.
- [ ] Invalid specs (hallucinated file paths, missing sections, vague placeholders) automatically loop back to `draft_spec_node` with exact error feedback.
- [ ] Validated specs and Q&A context are persisted in `decisions.py` and `ledger.py`.
- [ ] All tests in `tests/test_issue_generator.py` and `tests/test_cli.py` pass.

## Verification Commands
```bash
pytest tests/test_issue_generator.py tests/test_cli.py
```
````

---

## Issue 5: `[remote-server] Stage 3 (Init Coder Agent): Worktree-Isolated Coding Graph with td Deterministic Gates & Validated Re-Planner`

````markdown
# [remote-server] Stage 3 (Init Coder Agent): Worktree-Isolated Coding Graph with td Deterministic Gates & Validated Re-Planner

## Problem Statement
`deep-agent/coder.py` needs to be refactored into a strict LangGraph state machine (`InitCoderGraph`) that:
1. Starts by calling `td worktree create --issue <id> --json` to provision an isolated, dependency-warm worktree.
2. Executes the issue's implementation plan using file tools strictly sandboxed to that worktree path.
3. Runs deterministic `td` gates (`td verify-scope --auto-revert --json`, `td audit-code --json`, and the issue's `Verification Commands`) after every coding turn.
4. Routes to a **Validated Re-Planner Node** whenever deterministic checks fail twice in a row, updating the plan in persistent memory (`decisions.py`) and re-validating the updated plan against `td validate-issue --json` before resuming coding.

## Objective
Refactor `deep-agent/coder.py` into `InitCoderGraph` with hard `td` worktree isolation, automated scope/anti-slop gating, and a validated re-planning loop.

## Scope & File Boundaries

### Allowed Files (Modify)
- `deep-agent/coder.py`
- `deep-agent/s5tools.py`
- `deep-agent/decisions.py`
- `deep-agent/cli.py`
- `tests/test_coder.py`

### Forbidden Files
- `frontend/**`
- `backend/api/**`

## Implementation Plan

1. **Enforce Worktree Jail in `s5tools.py`:**
   - Require a mandatory `worktree_root: Path` parameter when constructing coding tools (`write_file`, `edit_file`, `read_file`, `run_command`).
   - Any attempt to resolve a path outside `worktree_root` raises `WorktreeBoundaryError`.

2. **Build `InitCoderGraph` State Machine (`deep-agent/coder.py`):**
   - **`worktree_init_node` (Deterministic):**
     - Calls `td validate-issue --file <spec> --json` to load `allowed_files`, `forbidden_files`, and `verification_commands`.
     - Calls `td worktree create --issue <issue_id> --repo-root <repo_root> --json` and binds `worktree_root`.
   - **`coder_step_node` (Scoped Coding Agent):**
     - Executes code edits inside `worktree_root` following the current `active_plan`.
   - **`td_deterministic_gate_node` (Deterministic Gate):**
     - Runs 3 sequential checks via `td` inside `worktree_root`:
       1. `td verify-scope --spec-file <spec> --worktree <worktree_root> --auto-revert --json` (reverts any unauthorized file edits immediately and flags violation).
       2. `td audit-code --worktree <worktree_root> --json` (blocks lint/type suppressions, syntax errors, and AST anti-patterns).
       3. Executes each command in `verification_commands` (`pytest`, `vitest`, `tsc`, etc.).
     - Sets `gate_passed = True` only if all 3 return zero violations and exit code 0.
   - **`validated_replanner_node` (Feedback & Plan Update Loop):**
     - Triggered when `gate_passed == False` and `consecutive_failures >= 2` (or identical error signature repeats).
     - Inspects the failing test/lint output + RAG context (`td rag query --json`) + prior decisions (`decisions.py`).
     - Produces an updated implementation plan, validates that the updated plan still conforms to the issue spec via `td validate-issue --json`, records the revised plan in `decisions.py`, resets `consecutive_failures = 0`, and routes back to `coder_step_node`.

3. **Write Unit & Graph Loop Tests (`tests/test_coder.py`):**
   - Test 1: Verifies `worktree_init_node` calls `td worktree create --json` and sandboxes all file writes to `worktree_root`.
   - Test 2: Verifies out-of-scope edits are auto-reverted by `td verify-scope --auto-revert --json` and blocked.
   - Test 3: Verifies 2 consecutive test failures route to `validated_replanner_node`, which persists a revised validated plan to `decisions.py` and succeeds on the next coding turn.

## Acceptance Criteria
- [ ] `InitCoderGraph` provisions worktrees exclusively via `td worktree create --json` and blocks any file access outside `worktree_root`.
- [ ] `td_deterministic_gate_node` enforces `td verify-scope`, `td audit-code`, and issue `verification_commands` on every turn.
- [ ] Repeated deterministic failures trigger `validated_replanner_node`, updating and validating the plan in `decisions.py`.
- [ ] All tests in `tests/test_coder.py` pass.

## Verification Commands
```bash
pytest tests/test_coder.py tests/test_delegation_guard.py
```
````

---

## Issue 6: `[boomtick + remote-server] Stage 4 (Issue + Code Auditor Agent): Deterministic-Gated Deep Semantic, Impact & Visual Auditor`

````markdown
# [boomtick + remote-server] Stage 4 (Issue + Code Auditor Agent): Deterministic-Gated Deep Semantic, Impact & Visual Auditor

## Problem Statement
Currently, visual diffing, DOM impact analysis, and anti-slop auditing are split between `boomtick/scripts/impact-*.ts`, `boomtick/lib/codeReviewOrchestrator.ts`, and `remote-server/deep-agent/impact.py` + `ux_audit.py`.

We need to:
1. Standardize the deterministic and visual impact runners behind `td audit-code --json` and `td audit-impact --json` in `boomtick`.
2. Delete duplicate impact/UX runner code in `remote-server` where redundant.
3. Build `AuditorGraph` (`remote-server/deep-agent/auditor.py`) with a **strict deterministic gate**: it must never run deep LLM semantic review or visual browser diffing until `td verify-scope`, `td audit-code`, and all unit tests pass 100%. Once gated checks pass, it verifies visual impact, dependency blast radius, anti-slop/complexity compliance, and 1-to-1 Acceptance Criteria traceability.

## Objective
Expose `td audit-impact --json` in `boomtick` and implement the two-phase gated `AuditorGraph` in `remote-server/deep-agent/auditor.py`.

## Scope & File Boundaries

### Allowed Files (Create / Modify / Delete)
- `boomtick/cli/dev_tools/cli.py`
- `boomtick/cli/dev_tools/services/dependency_graph.py`
- `boomtick/cli/dev_tools/ux_report.py`
- `boomtick/cli/tests/test_ast_audit.py`
- `remote-server/deep-agent/auditor.py` (CREATE)
- `remote-server/deep-agent/impact.py` (DELETE)
- `remote-server/deep-agent/ux_audit.py` (DELETE)
- `remote-server/tests/test_impact.py` (DELETE)
- `remote-server/tests/test_ux_audit.py` (DELETE)
- `remote-server/tests/test_auditor.py` (CREATE)

### Forbidden Files
- `remote-server/frontend/**`

## Implementation Plan

1. **Standardize `td audit-impact --json` in `boomtick` (`boomtick/cli/dev_tools/cli.py`):**
   - Add `td audit-impact --worktree <path> [--base-ref origin/main] --json`:
     - Runs `dependency_graph.py` on changed files to compute downstream callers, blast radius, and architectural layer violations.
     - Detects if any UI/frontend files (`*.tsx`, `*.css`, `*.html`) changed. If so, invokes `boomtick`'s visual/DOM impact scripts (`scripts/check-visual-changes.ts`, `scripts/impact-dom-diff.ts`, `scripts/impact-visual-diff.ts`, `ux_report.py`) and includes DOM/screenshot diff metrics in the JSON output.
     - Returns `{"passed": bool, "changed_files": [...], "downstream_dependents": [...], "ui_changed": bool, "visual_diff": {...}}`.

2. **Delete Duplicate Impact & UX Scripts in `remote-server`:**
   - `git rm remote-server/deep-agent/impact.py remote-server/deep-agent/ux_audit.py remote-server/tests/test_impact.py remote-server/tests/test_ux_audit.py`.

3. **Implement Two-Phase Gated `AuditorGraph` (`remote-server/deep-agent/auditor.py`):**
   - **Node 1 (`hard_deterministic_gate_node` — Zero LLM Tokens):**
     - Runs `td verify-scope --worktree <path> --spec-file <spec> --json`.
     - Runs `td audit-code --worktree <path> --json` (enforcing `audit-ai-slop.py`, `detect-antipatterns.mjs`, `check-suppression-inventory.mjs`, and `ast_audit.py` complexity thresholds).
     - Runs all issue `Verification Commands`.
     - **Gate Condition:** If any check fails, immediately exit `AuditorGraph` with `status = "BLOCKED_DETERMINISTIC"` and zero LLM calls.
   - **Node 2 (`td_impact_and_visual_node` — Only runs if Node 1 passes):**
     - Calls `td audit-impact --worktree <path> --json` to gather AST dependency blast-radius and visual/DOM diff results.
   - **Node 3 (`deep_semantic_auditor_node` — LLM Requirement & Anti-Slop Verifier):**
     - Evaluates the `git diff`, `td audit-impact` payload, and original Issue Spec.
     - Produces a strict Pydantic `AuditVerdict`:
       - `acceptance_criteria_matrix`: maps every criterion in the issue to `{"criterion": str, "verified": bool, "file_line_evidence": str}`.
       - `anti_slop_and_best_practices`: checks for tautological tests (testing only mocks), dead abstractions, or unhandled edge cases in impacted downstream callers.
       - `visual_regression_verdict`: evaluates visual/DOM diffs if `ui_changed == True`.
       - `status`: `"APPROVED"` only if 100% of acceptance criteria are verified and zero slop/visual regressions exist; otherwise `"CHANGES_REQUESTED"` with actionable `repair_plan`.

4. **Write Verification Tests (`remote-server/tests/test_auditor.py`):**
   - Test 1 (Hard Gate Enforcement): When `td audit-code` or unit tests fail, assert `AuditorGraph` returns `BLOCKED_DETERMINISTIC` and makes **0 calls** to `td audit-impact` or the LLM.
   - Test 2 (Visual & Impact Integration): When Node 1 passes on a UI diff, assert `td audit-impact --json` runs and feeds visual/dependency data into `deep_semantic_auditor_node`.
   - Test 3 (Unmet Requirement Rejection): When an acceptance criterion lacks evidence in the diff, assert `status == "CHANGES_REQUESTED"` with specific repair instructions.

## Acceptance Criteria
- [ ] `remote-server/deep-agent/impact.py` and `ux_audit.py` are deleted; all impact and visual diffing runs via `td audit-impact --json`.
- [ ] `AuditorGraph` strictly gates Node 2 and Node 3 behind `hard_deterministic_gate_node`.
- [ ] `AuditVerdict` verifies every issue Acceptance Criterion with `file:line` evidence, dependency impact, visual impact, and anti-slop rules.
- [ ] All tests in `remote-server/tests/test_auditor.py` and `boomtick/cli/tests/test_ast_audit.py` pass.

## Verification Commands
```bash
# In boomtick:
pytest cli/tests/test_ast_audit.py cli/tests/test_dependency_graph.py

# In remote-server:
test ! -f deep-agent/impact.py && test ! -f deep-agent/ux_audit.py
pytest tests/test_auditor.py
```
````

---

## Issue 7: `[remote-server] End-to-End Deep Agent Pipeline Orchestrator & Verification Eval Suite`

````markdown
# [remote-server] End-to-End Deep Agent Pipeline Orchestrator & Verification Eval Suite

## Problem Statement
With `td` standardized as the single deterministic tool layer and the 4 scoped deep agents defined (`td init`, `IssueGeneratorGraph`, `InitCoderGraph`, `AuditorGraph`), we need a top-level LangGraph pipeline (`deep-agent/pipeline.py`) and an end-to-end evaluation harness (`deep-agent/eval_pipeline.py` & `tests/test_pipeline_e2e.py`) that proves the entire autonomous loop works from user intent to verified PR submission and cleans up worktrees upon completion.

## Objective
Create `deep-agent/pipeline.py` connecting all 4 stages with persistent SQLite/ledger checkpointing, Auditor-to-Coder repair routing, `td submit-pr` integration, and a deterministic E2E verification test suite.

## Scope & File Boundaries

### Allowed Files (Create / Modify)
- `deep-agent/pipeline.py` (CREATE)
- `deep-agent/eval_pipeline.py` (CREATE)
- `deep-agent/cli.py`
- `tests/test_pipeline_e2e.py` (CREATE)

### Forbidden Files
- `frontend/**`
- `caddy/**`

## Implementation Plan

1. **Implement Master Pipeline (`deep-agent/pipeline.py`):**
   - Orchestrates the 4 stages with persistent state in `deep-agent/runs.py` and `deep-agent/ledger.py`:
     1. **Stage 1 (`Init`):** Runs `td init --repo-root <path> --json`.
     2. **Stage 2 (`IssueGeneratorGraph`):** Investigates codebase via `td rag query` & AST graph, drafts PRD spec, and loops until `td validate-issue --json` passes.
     3. **Stage 3 (`InitCoderGraph`):** Calls `td worktree create --json`, implements the spec inside the isolated worktree, and loops with `validated_replanner_node` until `td verify-scope`, `td audit-code`, and `verification_commands` pass.
     4. **Stage 4 (`AuditorGraph`):** Confirms deterministic gate, runs `td audit-impact --json` (blast radius + visual diff), and audits requirement traceability.
   - **Closed-Loop Repair Routing:**
     - If `AuditorGraph` returns `CHANGES_REQUESTED` or `BLOCKED_DETERMINISTIC`, route `AuditVerdict.repair_plan` back into `InitCoderGraph` inside the **same worktree** (up to `max_audit_cycles = 2`).
     - If `AuditorGraph` returns `APPROVED`:
       - If `submit_pr=True`, invoke `td submit-pr --worktree <worktree_root> --issue <id> --json`.
       - Clean up temporary resources via `td worktree remove` once merged/submitted.

2. **Create E2E Verification Eval Suite (`deep-agent/eval_pipeline.py` & `tests/test_pipeline_e2e.py`):**
   - Build a self-contained git repository fixture with a real `origin/main` bare remote, sample Python/TS modules, a test suite, and `td` CLI integration to verify 3 end-to-end scenarios:
     - **Eval 1 (Full Autonomous Bugfix):** Input vague intent -> `IssueGeneratorGraph` queries RAG, identifies target file, and passes `td validate-issue` -> `InitCoderGraph` creates worktree, edits code, passes `td verify-scope` + `td audit-code` + `pytest` -> `AuditorGraph` verifies acceptance criteria and returns `APPROVED`.
     - **Eval 2 (Coder Failure -> Re-Plan Recovery):** Input task where initial coder attempt fails unit tests twice -> assert `validated_replanner_node` updates the plan in `decisions.py` and passes on the subsequent turn.
     - **Eval 3 (Auditor Rejection -> Coder Fix):** Input task where coder passes unit tests but misses 1 acceptance criterion -> assert `AuditorGraph` returns `CHANGES_REQUESTED`, routes back to `InitCoderGraph` in the same worktree, fixes the missing requirement, and achieves `APPROVED` on audit cycle 2.

3. **Expose CLI Commands (`deep-agent/cli.py`):**
   - Add `run-pipeline --intent "<prompt>" --repo <path> [--submit-pr]` and `eval-pipeline` subcommands.

## Acceptance Criteria
- [ ] `deep-agent/pipeline.py` executes all 4 stages end-to-end using exclusively `td` deterministic gates and worktrees.
- [ ] Auditor rejections (`CHANGES_REQUESTED`) route back to `InitCoderGraph` in the active worktree and re-verify.
- [ ] All 3 end-to-end evaluation scenarios in `tests/test_pipeline_e2e.py` and `deep-agent/eval_pipeline.py` pass with zero legacy `core_cli` dependencies.

## Verification Commands
```bash
pytest tests/test_pipeline_e2e.py tests/test_issue_generator.py tests/test_coder.py tests/test_auditor.py
python -m deep_agent.cli eval-pipeline
```
````
