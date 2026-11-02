# Architecture & Technical Debt Consolidation Issues for `arii/remote-server`

This document details the discrete, actionable GitHub issues generated to resolve technical debt, eliminate redundancy, and migrate `arii/remote-server` to a unified client-server monorepo architecture.

---

## Issue 1: Consolidate Setup and Bootstrap Scripts into Makefile / Unified Task Runner

### Title
`refactor(infra): consolidate setup and bootstrap bash scripts into root Makefile`

### Labels
`refactor`, `developer-experience`, `technical-debt`

### Context / Problem
The `scripts/` directory currently contains numerous disjointed bash scripts:
- `scripts/setup-boomtick-mcp.sh`
- `scripts/setup-browser-mcp.sh`
- `scripts/setup-browsermcp-tunnel.sh`
- `scripts/setup-gh-auth.sh`
- `scripts/setup-hooks.sh`
- `scripts/bootstrap.sh`

These standalone scripts duplicate environment verification (checking for Node, pnpm, Python, gh CLI, docker), logging patterns, and error handling boilerplate. Furthermore, developers and automation must invoke them independently without automated dependency ordering.

### Target Files
- `Makefile` (or `Taskfile.yml`)
- `scripts/setup-*.sh` (to be consolidated / pruned)
- `scripts/bootstrap.sh`
- `package.json` (update lifecycle script references)
- `README.md` (update setup documentation)

### Actionable Plan
1. Create a root `Makefile` providing unified targets:
   - `make bootstrap`: Runs base OS requirements check and invokes core setup steps.
   - `make setup-gh-auth`: Configures GitHub authentication and credential helpers.
   - `make setup-hooks`: Installs git hooks and runs hook verification.
   - `make setup-tunnel`: Configures the browser MCP tunnel and tokens.
   - `make setup-mcp`: Dependency chain executing `setup-gh-auth` and `setup-tunnel`, then setting up BoomTick MCP and Browser MCP configs.
   - `make check` / `make test` / `make lint`: Standardized validation entrypoints.
2. Centralize shared shell routines (prerequisite check, colored status logging) in a single helper or native Make directives.
3. Remove redundant standalone scripts once their capabilities are ported to Make targets.
4. Update `package.json` scripts and `README.md` quickstart instructions to reference the canonical Make targets.

### Acceptance Criteria
- Running `make setup-mcp` automatically handles auth, tunneling, and MCP configurations in correct dependency order.
- Standalone boilerplate scripts in `scripts/setup-*.sh` are retired.
- `make check` and `make lint` pass across the workspace.

---

## Issue 2: Unify Fragmented Python Executable Scripts into Structured CLI Package

### Title
`refactor(cli): unify buttons and scripts Python tooling into single CLI package`

### Labels
`refactor`, `python`, `architecture`

### Context / Problem
Python executable tooling is fragmented across disparate directories:
- `buttons/pi_logs.py`
- `buttons/briefing_summary.py`
- `scripts/repair_worker.py`
- `scripts/parse_task_spec.py`
- `deep-agent/` scripts (`ask_investigator.py`, `ledger.py`, etc.)

Each script maintains independent argument parsing (`argparse` or manual `sys.argv`), repeated environment configuration, and separate logging setups.

### Target Files
- `core_cli/` (or `cli/`) package directory
- `scripts/repair_worker.py`
- `scripts/parse_task_spec.py`
- `buttons/pi_logs.py`
- `buttons/briefing_summary.py`
- `pyproject.toml` or `requirements.txt`

### Actionable Plan
1. Establish a structured internal Python package (e.g., `core_cli/`):
   - `core_cli/__init__.py`
   - `core_cli/main.py`: Unified CLI entrypoint using `Click` or `Typer`.
   - `core_cli/commands/repair.py`: Autonomous repair worker execution and AST validation.
   - `core_cli/commands/task_spec.py`: Deterministic task specification parser.
   - `core_cli/commands/logs.py`: Pi session log parsing and rendering.
   - `core_cli/commands/briefing.py`: Briefing summary generation and CI status normalization.
2. Standardize configuration loading, working directory resolution, and error output handling across all subcommands.
3. Expose CLI subcommands (e.g. `pi-cli repair`, `pi-cli parse-spec`, `pi-cli logs`).
4. Replace invocation points in buttons server and terminal scripts with calls to the unified CLI.

### Acceptance Criteria
- All standalone utilities are invocable via a single entrypoint (e.g., `python3 -m core_cli.main <subcommand>`).
- Redundant argument parsing and path discovery code is removed.
- Full test suite in `tests/` passes with updated module imports.

---

## Issue 3: Consolidate Deep Agent Triage Logic into Single TriageAgent Class

### Title
`refactor(deep-agent): consolidate pr_triage_agent, triage_explainer, and triage_fanout`

### Labels
`refactor`, `agents`, `deep-agent`

### Context / Problem
The `deep-agent/` directory splits triage workflows across three overlapping scripts:
- `deep-agent/pr_triage_agent.py`
- `deep-agent/triage_explainer.py`
- `deep-agent/triage_fanout.py`

These scripts represent sequential phases of the PR triage lifecycle (discovering open PRs, fanning out evaluations, and generating triage summaries/explanations). They duplicate GitHub API querying via `gh`, LLM prompt construction, Ollama client initialization, and ledger recording.

### Target Files
- `deep-agent/triage.py` (or `deep-agent/triage_agent.py`)
- `deep-agent/pr_triage_agent.py` (to be deprecated/merged)
- `deep-agent/triage_explainer.py` (to be deprecated/merged)
- `deep-agent/triage_fanout.py` (to be deprecated/merged)
- `docs/autonomous-triage-runbook.md`

### Actionable Plan
1. Create a unified `TriageAgent` class in `deep-agent/triage.py`.
2. Encapsulate the triage lifecycle stages as methods:
   - `triage_agent.discover_and_fanout()`: Detect open PRs across active repositories and schedule triage checks.
   - `triage_agent.evaluate_pr(pr_number)`: Execute diff filtering, context pruning, and deterministic evaluation.
   - `triage_agent.explain(evaluation)`: Generate structured triage explanations and publish comments/ledger entries.
3. Reuse a shared LLM execution helper and ledger instance (`deep-agent/ledger.py`).
4. Update `docs/autonomous-triage-runbook.md` and automated scheduled runners to use `python -m deep_agent.triage`.

### Acceptance Criteria
- Triage execution runs from a single unified class without redundant state initialization.
- Re-executing triage does not perform duplicate GitHub API fetches or instantiate duplicate LLM client sessions.

---

## Issue 4: DRY GitHub Actions Workflows Using Reusable Composite Actions

### Title
`ci: extract common setup steps in workflows into reusable composite actions`

### Labels
`ci`, `github-actions`, `infrastructure`

### Context / Problem
The `.github/workflows/` directory contains repetitive boilerplates:
- `agent-orchestrator.yml`
- `ai-review.yml`
- `chatops-trigger.yml`
- `ci-repair.yml`
- `ci.yml`

Each workflow independently configures git checkouts, installs Python versions with caching, configures Node/pnpm, and authenticates Docker credentials.

### Target Files
- `.github/actions/setup-environment/action.yml` (new)
- `.github/workflows/agent-orchestrator.yml`
- `.github/workflows/ai-review.yml`
- `.github/workflows/chatops-trigger.yml`
- `.github/workflows/ci-repair.yml`
- `.github/workflows/ci.yml`

### Actionable Plan
1. Create composite action `.github/actions/setup-environment/action.yml` accepting inputs (`python-version`, `node-version`, `pnpm-cache`).
2. Consolidate checkout, Python setup, pnpm cache setup, and environment verification into this composite action.
3. Refactor all workflow files to invoke `uses: ./.github/actions/setup-environment` instead of inlining 20-30 lines of boilerplate.
4. Validate workflow syntax and execution paths with dry-run/linter.

### Acceptance Criteria
- All GitHub Actions workflows use `.github/actions/setup-environment`.
- Workflow definitions are DRY, reducing maintenance overhead across CI files.
- `ci.yml` and all other workflows continue passing on pull requests and pushes.

---

## Issue 5: Deduplicate Dependencies and Centralize Configuration Schema

### Title
`refactor(monorepo): deduplicate package dependencies and unify configuration files`

### Labels
`configuration`, `pnpm`, `monorepo`

### Context / Problem
Dependency management and application configurations are fragmented:
- Root `package.json` + `pnpm-lock.yaml` alongside `buttons/ux/package.json` + `buttons/ux/pnpm-lock.yaml`.
- Configurations scattered across `project_config.json`, `.env.example`, and `terminal/pi-config/` (`settings.json`, `mcp.json`, `models.json`).
This introduces dependency drift and potential configuration divergence across components.

### Target Files
- `pnpm-workspace.yaml` (root workspace config)
- `package.json`
- `buttons/ux/package.json`
- `config/schema.json` or unified project configuration loader
- `project_config.json`
- `terminal/pi-config/`

### Actionable Plan
1. Define a pnpm workspace with `pnpm-workspace.yaml` at root managing `buttons/ux/` and root packages under one lockfile.
2. Remove nested `buttons/ux/pnpm-lock.yaml` and resolve dependencies centrally.
3. Design a single validated configuration schema (JSON Schema or Pydantic model) unifying `project_config.json`, model defaults, and tool settings.
4. Provide a single config reader utility in Python and TypeScript to eliminate hardcoded duplicate paths.

### Acceptance Criteria
- Only one canonical `pnpm-lock.yaml` exists at the root of the repository.
- Shared settings are read from the unified configuration schema without state desync.

---

## Issue 6: Eliminate UI Redundancies and Consolidate S5 Dashboard Views

### Title
`fix(ux): eliminate duplicate views, redundant alert banners, and unstructured actions`

### Labels
`ui`, `ux`, `frontend`

### Context / Problem
The current UI exhibits several overlapping views and redundant alerts:
1. **Fractured Agent Session Tracking:** Jules/Agent sessions appear redundantly across the "Project" tab, "Briefing" tab, "Actions" tab, and "Pi agent" tab.
2. **Duplicated "Needs Attention" & Issue Alerts:** Pull requests requiring attention (such as PR #171) appear simultaneously in both the "Briefing" and "Actions" tabs.
3. **Double-Listed PRs in Briefing:** PRs can appear twice in the briefing list (e.g. once with a "Repair" tag and again marked "CONFLICTED").
4. **Redundant System Health Displays:** The "Actions" tab displays a top-level "System Health" widget alongside a "Status" action button and an accordion for system health.
5. **Overlapping Output/Logging Views:** A persistent bottom output terminal, a dedicated "Ledger" tab, and a "Logs" button compete for the same display information.
6. **Unstructured Action Buttons:** The "Actions" tab renders a long vertical dump of full-width buttons mixing infrastructure, agent, and workflow controls.

### Target Files
- `buttons/server.py` (UI template and JavaScript client handlers)
- `buttons/briefing_summary.py`

### Actionable Plan
1. **Single Source of Truth for Sessions:** Keep Jules/Agent sessions in a dedicated, unified panel or tab with clear filtering by status and repo. Remove duplicate session listings from other tabs.
2. **Unified Attention Inbox:** Deduplicate alerts so attention-worthy PRs and issues are displayed once in a centralized inbox view.
3. **Fix PR Briefing Deduplication:** Fix the briefing aggregation logic to combine repair status and merge conflict badges into a single PR entry.
4. **Consolidate System Health:** Merge the accordion, status button, and top health widget into a single cohesive system health component.
5. **Streamline Logging/Output:** Route terminal outputs and ledger audits into a structured drawer with tabs (Command Output | Audit Ledger) instead of multiple competing widgets.
6. **Categorized Action Button Grid:** Organize action buttons into a responsive, labeled grid categorized by domain:
   - *Infrastructure & Environment* (Recreate, Logs, Health)
   - *Agent Operations* (Sync Jules, Nudge, Repair)
   - *PR & Workflow Automation* (Preflight, Plan, Merge Approved)

### Acceptance Criteria
- No PR or alert is displayed multiple times within the same view or across conflicting banners.
- System health is displayed in a single unified card.
- Action buttons are grouped by category in a compact, responsive grid.
- Jules agent sessions have a single canonical management view.

---

## Issue 7: Client-Server Restructure: React + Vite Frontend & Unified FastAPI Backend

### Title
`feat(architecture): migrate remote-server to React + Vite frontend and FastAPI backend monorepo`

### Labels
`architecture`, `frontend`, `backend`, `fastapi`, `react`

### Context / Problem
`buttons/server.py` is an 8,900+ line monolithic Python file that serves inline HTML, client-side JavaScript, server-sent events, Docker orchestration, and subprocess automation. Concurrently, standalone automation scripts in `scripts/` and `deep-agent/` duplicate background job logic. This creates high maintenance overhead, state synchronization problems, and tight coupling between UI and server code.

### Target Architecture
```text
remote-server/
├── frontend/               # Replaces buttons/ux/ and embedded HTML/JS
│   ├── src/
│   │   ├── components/     # Reusable UI (buttons, widgets, log viewers)
│   │   ├── views/          # Tab views (Actions, Ledger, Project, Briefing)
│   │   └── store/          # Centralized React state for Agent/System status
│   ├── package.json
│   └── vite.config.ts
├── backend/                # Replaces scripts/, deep-agent/, buttons/server.py
│   ├── api/
│   │   └── routers/        # Endpoint groupings (repo, agents, system, chat)
│   ├── core/               # Shared utilities (config, docker client, git helpers)
│   ├── agents/             # Consolidated LLM, repair, and triage logic
│   ├── server.py           # Main FastAPI application entrypoint
│   └── requirements.txt
├── .github/                # Consolidated workflows and composite actions
└── docker-compose.yml
```

### Actionable Plan
1. **Backend Migration to FastAPI:**
   - Establish `backend/server.py` with modular FastAPI routers:
     - `/api/system`: Health metrics, Docker container management, service recreation.
     - `/api/repo`: Discovered repositories, PR listing, branch/status inspection.
     - `/api/agents`: Jules session listing/nudge, repair worker triggers, background jobs.
     - `/api/chat`: SSE-based interactive streaming chat with cancellation.
   - Offload long-running tasks (repair worker, multi-PR conflict checks) to background tasks with structured polling/SSE updates.
   - Enforce Pydantic request/response schemas for automatic OpenAPI documentation.
2. **Frontend Migration to React + Vite:**
   - Initialize `frontend/` using Vite + React + TypeScript + Tailwind CSS.
   - Centralize client state using a global store (Zustand or React Context) tracking selected repository, active jobs, and agent sessions to eliminate tab desync.
   - Implement modular components (`<AgentSessionManager />`, `<SystemHealthCard />`, `<ActionGrid />`, `<OutputDrawer />`).
3. **Docker & Compose Updates:**
   - Update `docker-compose.yml` to build and serve the FastAPI backend and mount the static React build or serve it behind Caddy reverse proxy.
   - Maintain seamless OAuth2-proxy integration and Caddy route prefixing.

### Acceptance Criteria
- Monolithic `buttons/server.py` is decomposed into modular FastAPI routers and a clean React frontend.
- Single source of truth for repository selection and agent status across the entire application.
- Modern build tooling with typed API contracts and automated OpenAPI schema generation.
