# Deep Agent Consolidation & Verification — Next Steps & Codebase Audit Guide
**Target Commits Audited:** `boomtick @ 37524e910081` | `remote-server @ 9bb46c4ab102`

---

## 1. Executive Summary: What Landed vs. What Remains

### Completed Milestones
1. **Jules & Unsafe MCP Tool Purge (`boomtick`):** `mcp/src/tools/jules/*`, `repo.check_jules_status.ts`, `github.checkout_branch.ts`, `repo.create_branch.ts`, `github.create_pull_request.ts`, and `agent.discuss/dispatch/logs/status` are deleted.
2. **Deterministic `td` CLI Gates (`boomtick`):** `cli/dev_tools/cli.py` now exposes `td init`, `td rag index/query`, `td validate-issue`, `td verify-scope`, `td audit-code`, and `td audit-impact` with `--json` output.
3. **4-Stage Deep Agent Pipeline (`remote-server`):** `deep-agent/issue_generator.py`, `deep-agent/coder.py` (`InitCoderGraph`), `deep-agent/auditor.py` (`AuditorGraph`), `deep-agent/pipeline.py`, and `deep-agent/eval_pipeline.py` are implemented and verified.
4. **`core_cli/` Directory Removed (`remote-server`):** The top-level `core_cli/` directory and `deep-agent/build_index.py` / `search_docs.py` are gone.

### Critical Gaps Discovered in the Codebase Audit (Next Steps)
Despite the completions above, several **duplicate modules, legacy MCP tools, and orphaned `core_cli` callers** remain in the trees:

> Status (audit #1095, 2026-11-06): gaps 2–4 below are SUPERSEDED. `run_core_cli` is gone everywhere (`tests/test_no_core_cli.py` locks zero `core_cli`/`run_core_cli` references across `backend/`, `deep-agent/`, `tests/`, `.github/`, `Makefile`, `mk/`), `ci-repair.yml` was removed, and the `backend/` + `deep-agent/` modules named below are still imported live — not dead duplicates. Do not execute Steps 3.1–3.4; they are kept as historical record only.
1. **`boomtick/mcp` still exposes 23 tools instead of $\le 16$** (and `mcp/src/mcp/server.test.ts` still asserts `<= 43` tools) because 6 redundant/unsafe tools were kept and the 6 new deterministic `td` gates (`td init`, `td rag`, `td validate-issue`, `td verify-scope`, `td audit-code`, `td audit-impact`) are **not yet exposed as MCP tools** in `mcp/src/mcp/definitions.ts`.
2. **`remote-server/backend/` secretly retained copies of `core_cli/`** as top-level files inside `backend/` (`backend/ast_validator.py`, `backend/parse_task_spec.py`, `backend/verify_allowlist.py`, `backend/impact_pass.py`, `backend/merge_gated.py`, `backend/review.py`, `backend/repair_worker.py`, `backend/ts_check.js`, etc.) and `backend/core/__init__.py` still routes `run_core_cli()` to `python -m backend.<module>` instead of `td <subcommand> --json`.
3. **`remote-server/deep-agent/impact.py` and `ux_audit.py` were not deleted** (along with `tests/test_impact.py`, `tests/test_ux_audit.py`, and `tests/test_core_cli.py`).
4. **CI Workflows & Makefiles in `remote-server` still invoke `python3 -m core_cli`** (`.github/workflows/ai-review.yml`, `.github/workflows/ci-repair.yml`, `.github/workflows/cutoff-proof.yml`, `Makefile`, `mk/common.mk`), which will crash in CI because `core_cli/` no longer exists.
5. **Dual Vector Indexing in `boomtick`:** `cli/dev_tools/cli.py` added inline `_rag_index_repo()` / `_rag_query_repo()` functions writing JSON to `.td_rag/index.json` while `cli/dev_tools/services/vector_store.py` (`SqliteVecStore` writing to `.vector_cache/index.db`) remains separate and unused by `td rag`.

---

## 2. MCP Tool Inventory (`boomtick/mcp`): Exact Keep / Remove / Add Matrix

Currently, `boomtick/mcp/src/mcp/definitions.ts` and `mcp/src/mcp/tools.ts` register **23 tools**, and `mcp/src/mcp/server.test.ts:138` still asserts:
```ts
// boomtick/mcp/src/mcp/server.test.ts:138
expect(result.tools.length).toBeLessThanOrEqual(43);
```

To make `boomtick-mcp` a clean, worktree-safe, 16-tool deterministic engine, execute the following changes:

### A. REMOVE (Delete these 11 MCP Tools & Files)

| MCP Tool Name | File in `boomtick/mcp/src/tools/` | Why Remove & Specific Code Reference |
| :--- | :--- | :--- |
| `repo.commit_patch` | `repo.commit_patch.ts` | **Violates Worktree Isolation:** Runs `git checkout -B` and `git apply` directly in `config.targetRepoPath` (root repo) instead of an isolated `.worktrees/issue-<id>` directory (`repo.commit_patch.ts:27-35`). Agents must edit inside a worktree and use `agent.submit_pr`. |
| `repo.multi_exec` | `repo.multi_exec.ts` + `.test.ts` | **Arbitrary Unsandboxed Shell Execution:** Runs `Promise.all(commands.map(cmd => execAsync(cmd, { cwd: config.targetRepoPath })))` (`repo.multi_exec.ts:18-23`) with zero worktree jailing or allowlist validation. |
| `repo.get_package_scripts` | `repo.get_package_scripts.ts` + `.test.ts` | **Redundant:** `repo.read_agent_context.ts:24-39` already reads and returns `packageScripts` from `project_config.json` / `package.json`. |
| `repo.get_changed_files` | `repo.get_changed_files.ts` | **Superseded by `td.verify_scope` & `td.audit_impact`:** Runs raw `git status --porcelain` on the root repo (`repo.get_changed_files.ts:13`). `td verify-scope --json` and `td audit-impact --json` provide worktree-aware changed files + scope/blast-radius analysis. |
| `repo.get_command_schema` | `repo.get_command_schema.ts` | **Redundant Meta-Tool:** Introspects `td` argparse help text (`repo.get_command_schema.ts:11-28`), which is unnecessary once MCP tools have native Zod schemas in `contract.ts`. |
| `repo.logs` | `repo.logs.ts` | **Legacy Log Tailer:** Tails `/tmp` or root log files (`repo.logs.ts:14-30`), left over from the deleted `agent.logs` workflow. |
| `github.get_pr_commits` | `github.get_pr_commits.ts` | **Granular PR Bloat:** Consolidate into `github.get_pr.ts` (which already fetches PR metadata via `td pr view`). |
| `github.get_pr_reviews` | `github.get_pr_reviews.ts` + `.test.ts` | **Granular PR Bloat:** Consolidate into `github.get_pr.ts`. |
| `github.get_merge_conflict_files` | `github.get_merge_conflict_files.ts` | **Granular PR Bloat:** Consolidate conflict file detection into `github.get_pr.ts` or `td.init`. |
| `skill.list` | `skill.list.ts` | **Consolidate:** Fold skill listing into `repo.read_agent_context.ts` so the agent gets available skill names in its initial single-call bootstrap payload. |
| `skill.read` | `skill.read.ts` + `skill.access.test.ts` | **Consolidate:** Allow `repo.read_agent_context({ skillName?: string })` to optionally return a specific skill's `SKILL.md` content. |

### B. KEEP (Retain these 12 Core Worktree-Safe Tools)

| # | MCP Tool Name | Implementation File | Role in Deep Agent Architecture |
| :--- | :--- | :--- | :--- |
| 1 | `repo.read_agent_context` | `mcp/src/tools/repo.read_agent_context.ts` | Single-call bootstrap returning `.agents/AGENT_CONTRACT.md`, `packageScripts`, RAG index status (`.td_rag/meta.json` + `.vector_cache/index.db`), and skills list. |
| 2 | `worktree.create` | `mcp/src/tools/worktree.create.ts` | Provisions warm, dependency-linked worktree via `td worktree create --json` + `dependency_cache.ts`. |
| 3 | `worktree.list` | `mcp/src/tools/worktree.list.ts` | Lists active isolated git worktrees via `td worktree list --json`. |
| 4 | `worktree.remove` | `mcp/src/tools/worktree.remove.ts` | Tears down worktree and branch after PR submission. |
| 5 | `agent.submit_pr` | `mcp/src/tools/agent.submit_pr.ts` | Runs pre-PR gates, commits worktree changes, pushes branch, and opens PR via `td submit-pr --json`. |
| 6 | `repo.run_tests` | `mcp/src/tools/repo.run_tests.ts` | Runs project test suites inside the specified `worktreePath` (update `repo.run_tests.ts:15` to require `worktreePath` instead of `config.targetRepoPath`). |
| 7 | `github.issue_view` | `mcp/src/tools/github.issue_view.ts` | Fetches GitHub issue body/comments via `td issue view`. |
| 8 | `github.create_issue` | `mcp/src/tools/github.create_issue.ts` | Publishes validated PRD/Spec issues (used by `persist_and_publish_node` in `issue_generator.py`). |
| 9 | `github.issue_update` | `mcp/src/tools/github.issue_update.ts` | Updates issue body/labels/state. |
| 10 | `github.issue_comment` | `mcp/src/tools/github.issue_comment.ts` | Posts audit verdicts or triage summaries to an issue/PR. |
| 11 | `github.get_pr` | `mcp/src/tools/github.get_pr.ts` | Fetches PR metadata, diff summary, reviews, and merge-conflict files (absorbs `get_pr_diff`, `get_pr_commits`, `get_pr_reviews`, `get_merge_conflict_files`). |
| 12 | `repo.read_ci_logs` | `mcp/src/tools/repo.read_ci_logs.ts` | Fetches failing GitHub Actions workflow runs and log excerpts (absorbs `repo.get_workflow_runs.ts`). |

*(Note: Fold `github.get_pr_diff.ts`, `github.search_open_prs.ts`, and `repo.get_workflow_runs.ts` into `github.get_pr` and `repo.read_ci_logs` so total kept tools = 10–12).*

### C. ADD (Expose the 4 Deterministic `td` Engine Gates as MCP Tools)

Currently, `boomtick/cli/dev_tools/cli.py` implements the deterministic gates (`cmd_init` at line 588, `cmd_rag` at line 614, `cmd_validate_issue` at line 631, `cmd_verify_scope` at line 646, `cmd_audit_code` at line 676, `cmd_audit_impact` at line 732), but **none of them are exposed in `boomtick/mcp/src/mcp/definitions.ts`**. Add these 4 MCP tool wrappers using `runTdCliJson` from `mcp/src/lib/tdCli.ts`:

| # | New MCP Tool Name | File to Create | Underlying `td` Command (`cli/dev_tools/cli.py`) |
| :--- | :--- | :--- | :--- |
| 13 | `td.init_and_rag` | `mcp/src/tools/td.init_and_rag.ts` | Wraps `td init --repo-root <path> --json` and `td rag query --repo-root <path> --query <q> --top-k <k> --json`. |
| 14 | `td.validate_issue` | `mcp/src/tools/td.validate_issue.ts` | Wraps `td validate-issue --file <path> --repo-root <path> --json` (`spec_issue.py`). |
| 15 | `td.verify_scope` | `mcp/src/tools/td.verify_scope.ts` | Wraps `td verify-scope --spec-file <path> --worktree <path> [--auto-revert] --json` (`scope_gate.py`). |
| 16 | `td.audit_worktree` | `mcp/src/tools/td.audit_worktree.ts` | Wraps `td audit-code --worktree <path> --json` and `td audit-impact --worktree <path> --json` (`ast_audit.py` + `dependency_graph.py` + `ux_report.py`). |

After updating `mcp/src/mcp/definitions.ts` and `mcp/src/mcp/tools.ts`:
1. Run `pnpm --filter mcp run sync-schemas` (`mcp/scripts/sync-mcp-schemas.ts` & `mcp/scripts/sync-contracts.ts`) to regenerate `mcp/src/tools/contract.ts` and `cli/dev_tools/models/contracts.py`.
2. Update `mcp/src/mcp/server.test.ts:138` from `expect(result.tools.length).toBeLessThanOrEqual(43)` to `expect(result.tools.length).toBeLessThanOrEqual(16)`.

---

## 3. Step-by-Step Code Cleanup Instructions in `remote-server`

### Step 3.1: Delete Shadow `core_cli` Files Hidden Inside `remote-server/backend/` — SUPERSEDED (#1095)

> Do not run the `git rm` commands below: every listed file is still imported live. The `core_cli` cutover this step assumed is complete and test-locked (`tests/test_no_core_cli.py`, 4/4 green).
When `core_cli/` was removed, 9 of its modules were copied into `remote-server/backend/` instead of cutting callers over to `td`.

**Delete these 9 duplicate files in `remote-server`:**
```bash
git rm \
  backend/ast_validator.py \
  backend/parse_task_spec.py \
  backend/verify_allowlist.py \
  backend/impact_pass.py \
  backend/merge_gated.py \
  backend/preflight_digest.py \
  backend/repair_worker.py \
  backend/review.py \
  backend/ts_check.js
```
*(Keep `backend/briefing_summary.py`, `backend/notify_check.py`, `backend/autofile_scan.py`, `backend/context_filter.py`, and `backend/validate_config.py` only if used by FastAPI routers, or move them into `backend/notify/` and `backend/api/routers/briefing.py`).*

**Delete the remaining duplicate files in `remote-server/deep-agent/` and `tests/`:**
```bash
git rm \
  deep-agent/impact.py \
  deep-agent/ux_audit.py \
  tests/test_impact.py \
  tests/test_ux_audit.py \
  tests/test_core_cli.py \
  tests/test_ast_validator.py \
  tests/test_parse_task_spec.py \
  tests/test_verify_allowlist.py \
  tests/test_review.py \
  tests/test_repair_worker.py
```

### Step 3.2: Refactor `remote-server/backend/core/__init__.py` to Remove `run_core_cli`
In `remote-server/backend/core/__init__.py:345-393`, `run_core_cli()` still maps subcommands to `backend.<module>` (lines 352–365: `"parse-task-spec": "backend.parse_task_spec"`, `"verify-allowlist": "backend.verify_allowlist"`, `"ast-validator": "backend.ast_validator"`, etc.) and only falls back to `run_td()` when the subcommand is not in `module_map`.

**Action Required in `backend/core/__init__.py`:**
1. Delete `run_core_cli()` (lines 345–393) and remove `"run_core_cli"` from `__all__` (line 428).
2. Keep `run_td(args: list[str], cwd: Path | None = None, check: bool = False) -> dict[str, Any]` (lines 299–342) as the single entry point for deterministic tooling.

### Step 3.3: Cut Over Remaining `backend/` Callers from `run_core_cli` to `run_td`
Update these exact lines in `remote-server/backend/`:

1. **`backend/lane_dispatch.py:72-79`:**
   - **Current Code:**
     ```python
     rc, out, err = core.run_core_cli(
         ["parse-task-spec", "--issue", str(issue_num), "--repo", repo]
     )
     ```
   - **Replace With:** Fetch issue body via `gh.issue_view(repo, issue_num)` and pass to `core.run_td(["validate-issue", "--file", "-", "--repo-root", str(repo_path)])`.

2. **`backend/pr_gates.py:67-84`:**
   - **Current Code:** Imports `verify_allowlist` from `backend` (line 70) and falls back to `core.run_core_cli(["verify-allowlist", "--repo", repo, "--pr", str(pr_number)])` (line 75).
   - **Replace With:** Call `core.run_td(["verify-scope", "--spec-file", str(spec_path), "--worktree", str(worktree_path)], cwd=worktree_path)`.

3. **`backend/exec/agent_turn.py:30-31` & `236-267`:**
   - **Current Code:** Imports `ast_validator` and `context_filter` from `backend` (lines 30–31) and runs `ast_validator.validate_files(...)` post-turn.
   - **Replace With:** Call `core.run_td(["audit-code", "--worktree", str(worktree_dir)], cwd=worktree_dir)`.

4. **`backend/exec/engine.py:364-370` (`repair_sweep`):**
   - **Current Code:** Calls `core.run_core_cli(["repair-worker", "--repo", repo, "--pr", str(pr_num)])`.
   - **Replace With:** Call `core.run_td(["ci-repair", "--repo", repo, "--pr", str(pr_num)])` (backed by `boomtick/cli/dev_tools/services/repair_service.py`).

### Step 3.4: Fix Broken `core_cli` References in `remote-server` Workflows & Makefiles
The following files in `remote-server` still invoke `python3 -m core_cli` or reference deleted `core_cli/` paths and will fail in CI:

1. **`.github/workflows/ai-review.yml:40`:**
   - Replace `python3 -m core_cli review --pr "${{ github.event.pull_request.number }}" --repo "${{ github.repository }}"` with `td pr review "${{ github.event.pull_request.number }}" --repo "${{ github.repository }}" --json`.
2. **`.github/workflows/ci-repair.yml:39`:**
   - Replace `python3 -m core_cli repair-worker --run-id "${{ github.event.workflow_run.id }}" --repo "${{ github.repository }}"` with `td ci-repair --run-id "${{ github.event.workflow_run.id }}" --repo "${{ github.repository }}" --json`.
3. **`.github/workflows/cutoff-proof.yml:22-25`:**
   - Remove `core_cli/*.py` from the `wc -l` line-budget check and replace with `td audit-code --worktree . --json`.
4. **`Makefile:65-72` & `mk/common.mk:31`:**
   - In `Makefile:65`, replace `$(PYTHON) -m backend.validate_config` / `core_cli` comments with `td verify-infra --json` and `td audit-code --worktree . --json`.
   - In `mk/common.mk:31`, remove `CORE_CLI := $(PYTHON) -m backend`.

---

## 4. Step-by-Step Code Cleanup Instructions in `boomtick`

### Step 4.1: Unify `td rag` (`cli.py`) with `SqliteVecStore` (`services/vector_store.py`)
In `boomtick/cli/dev_tools/cli.py:345-481`, `_rag_index_repo()` and `_rag_query_repo()` currently implement a standalone TF-IDF / JSON-file indexer in `.td_rag/index.json` instead of using `SqliteVecStore` in `boomtick/cli/dev_tools/services/vector_store.py:1-215`.
- **Action:** Move the incremental git-diff SHA tracking (`last_indexed_sha` in `.td_rag/meta.json` at `cli.py:380-415`) and AST/Markdown chunking (`_chunk_python_file` and `_chunk_text_file` at `cli.py:296-343`) directly into `cli/dev_tools/services/vector_store.py`.
- Have `cmd_rag` (`cli.py:614-628`) and `cmd_init` (`cli.py:588-611`) instantiate `VectorStoreService` from `cli/dev_tools/services/vector_store.py` so there is only **one** vector index implementation in `boomtick`.

### Step 4.2: Remove Remaining Legacy `jules` References in `boomtick/cli`
While `mcp/src/tools/jules/*` was deleted, `boomtick/cli/dev_tools/cli.py` and `orchestrator.py` still contain legacy `jules` CLI flags and label handlers:
- `cli/dev_tools/cli.py:926-961`: Remove `jules` subcommand parser and `--jules` flags on `issue triage` / `pr review`.
- `cli/dev_tools/orchestrator.py:45-120`: Remove `jules` session polling fallback in `OrchestratorService`.
- `cli/dev_tools/constants.py`: Remove `JULES_*` label constants.

---

## 5. Verification Checklist for Next Steps

Run the following verification commands across both repositories once the above steps are applied:

```bash
# 1. Verify boomtick MCP tool count is <= 16 and schemas are in sync
cd boomtick
pnpm --filter mcp run sync-schemas
pnpm --filter mcp test
node scripts/verify-schemas.mjs
pytest cli/tests/test_td_gates.py cli/tests/test_td_rag.py cli/tests/test_td_worktree.py

# 2. Verify zero shadow core_cli or duplicate impact/ux modules remain in remote-server
cd ../remote-server
test ! -f backend/ast_validator.py && test ! -f backend/parse_task_spec.py && test ! -f backend/verify_allowlist.py
test ! -f deep-agent/impact.py && test ! -f deep-agent/ux_audit.py
! grep -rn "run_core_cli" backend/ deep-agent/ tests/
! grep -rn "core_cli" .github/ Makefile mk/ backend/ deep-agent/

# 3. Run full remote-server + deep-agent E2E pipeline suite
pytest backend/tests/ tests/test_issue_generator.py tests/test_coder.py tests/test_auditor.py tests/test_pipeline_e2e.py
python3 -m deep_agent.cli eval-pipeline
```
