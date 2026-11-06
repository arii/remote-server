# Duplicate-module purge analysis (cli_guide.md §2.1 vs tree evidence)

Date: 2026-11-06. Method: existence checks + live-importer grep on
`origin/main` (remote-server @ 341046f / #993, boomtick @ ab8fb41 / #1128).
Question: which §2.1 "Removed Components" rows are actually removable.

## Verdict up front

3 of 5 rows are already finished (nothing to do). The remaining 7 files
all have live callers — deleting them breaks shipped features, exactly
as recorded in `docs/archive/guide.md` Issue 6 ("NOT done — live
callers… Deletion would break shipped features") and
`docs/follow-ups.md` §4. There are no `td` equivalents to migrate to
(`td` subcommands today: `agent`, `audit-code`, `audit-impact`,
`context-warm`, `gh`, `init`, `rag`, `session`, `skill`,
`validate-issue`, `verify-scope`, `worktree` — no repair, no UX
capture, no spec parsing).

## Row-by-row evidence

### 1. `lane-orchestrator` / `lane-coder` backend modules — DO NOT EXIST

- `backend/lane_orchestrator.py`: absent.
- `backend/lane_coder.py`: absent.
- The lane system lives in memory (`lanes/registry.md`, `lanes/direction.md`)
  plus `.agents/skills/lane-coder/SKILL.md` and
  `.agents/skills/lane-orchestrator/SKILL.md` (the only 4 tracked skill
  files besides the two audit skills). There is no backend module to purge.
- NOTE: removing the lane *roles* would shut down the active multi-session
  operation (orchestrator + coders + browser agent currently working).
  The table names backend paths, so this is out of its scope.

### 2. Eleven MCP tool files — ALREADY PURGED by boomtick #1126

All absent from `boomtick/mcp/src/tools/`: `repo.multi_exec.ts`,
`repo.commit_patch.ts`, `repo.logs.ts`, `repo.get_package_scripts.ts`,
`repo.get_changed_files.ts`, `repo.get_command_schema.ts`,
`github.get_pr_commits.ts`, `github.get_pr_reviews.ts`,
`github.get_merge_conflict_files.ts`, `skill.list.ts`, `skill.read.ts`.
Current registry (`mcp/src/mcp/definitions.ts`, `BASE_MCP_TOOLS`) is
17 tools: 12 kept + 4 `td_*` gates + pre-existing `boomtick_health`
(see `docs/follow_up_guide.md` §2 for the matrix). `mcp` vitest: 246 passed.

### 3. "Unrestricted Chat Baselines" — NO ACTIONABLE TARGET

No file or symbol named; not implementable as stated.

### 4. Seven Python modules — ALL LOAD-BEARING (do not delete)

| File | Live importers (verified by grep) | Breaks if deleted |
|---|---|---|
| `backend/parse_task_spec.py` | `backend/api/routers/issues.py:40` (`parse_task_spec` import, used line 421); `backend/repair_worker.py:33`; `tests/test_ux_audit.py` | issues API spec parsing + repair dispatch |
| `backend/verify_allowlist.py` | `backend/api/routers/issues.py:41`; `backend/repair_worker.py:38`; `tests/test_ux_audit.py` | issues API + repair dispatch |
| `backend/ast_validator.py` | `backend/repair_worker.py:32` (`validate_source_syntax`) | repair dispatch |
| `backend/repair_worker.py` | spawned as subprocess by `issues.py:281` (`python3 -m backend.repair_worker --issue N`) | all repair dispatch; no `td repair` exists |
| `backend/impact_pass.py` | jobs engine `impact-pass` action step via `python -m backend.impact_pass` (see `backend/tests/test_jobs_exec.py:2383`); module docstring: "Invoked by the engine's impact-pass action step; never imported by the API" | scheduled #869 UX-regression job |
| `deep-agent/impact.py` | `deep-agent/deep_chat.py:70` (`run_ux_capture`, `resolve_capture_dir`, `diff_versions`, `collect_capture`, `UX_DIR`, lines 583–644); `deep-agent/ask_investigator.py:34` (lines 294–356) | UX capture/diff in chat + investigator |
| `deep-agent/ux_audit.py` | `backend/api/routers/jobs.py:441` (`_ux_audit_module`, lazy `import ux_audit`) | jobs exec for ux-audit jobs |

Notes:

- `repair_worker.py` keeps itself stdlib-only with a three-way import
  fallback (lines 32–65); it is the repair execution engine, not a copy
  of one. Deleting it strand
...[truncated 1587 chars]
