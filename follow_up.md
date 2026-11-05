# Deep Agent Co-Browsing & Dynamic Skills — Post-Consolidation Specification Issues

## Post-Merge Verification Audit: Issues 1–7 Status & Identified Gaps

All 7 prerequisite consolidation PRs have officially merged into `main` across both repositories:
- **`boomtick`**:
  - `PR #1120` (`[issue-1] Hard-delete legacy Jules/MCP tools, standardize td JSON gates`): **MERGED**.
  - `PR #1122` (`[issue-3] td rag index/query, td init, warm issue worktrees`): **MERGED**.
  - `PR #1115` (`chore: release main v0.19.2`): **MERGED**.
- **`remote-server`**:
  - `PR #976` (`[issue-2] Delete core_cli, relocate modules to backend, cut over to td`): **MERGED**.
  - `PR #978` (`[issue-3] retrieval: delegate queries to td rag, drop duplicate indexer`): **MERGED**.
  - `PR #979` (`[issue-4] IssueGeneratorGraph closed-loop spec agent`): **MERGED**.
  - `PR #980` (`[issue-5] InitCoderGraph worktree-isolated coding graph`): **MERGED**.
  - `PR #981` (`[issue-6] gated AuditorGraph + td audit-impact dependents gate`): **MERGED**.
  - `PR #982` (`[issue-7] pipeline orchestrator + E2E eval suite`): **MERGED**.
  - `PR #983` (`Docs: sync runbooks with Issues 1-7 code changes`): **MERGED**.

### Critical Deficiencies & Gaps Identified in the Merged PRs
During the live audit of the newly merged code, the following 5 deficiencies were identified and are directly targeted for remediation in Issues 8–12:

1. **`boomtick` `td audit-impact` Lacks Visual & DOM Diffs**:
   * *Observed State*: `cli/dev_tools/cli.py` (`audit_impact_gate`) only runs `ASTAuditor()` on changed files to compute static blast radius (`_impact_dependents`).
   * *Gap*: It contains zero DOM extraction, zero screenshot comparisons, and zero browser automation. It does not connect to any browser or CDP runtime.
   * *Resolution*: Issue 9 standardizes the remote CDP driver to supply `visual_diff` objects inside `td audit-impact`.

2. **Residual Jules Command Group in `boomtick` CLI**:
   * *Observed State*: `cli/dev_tools/cli.py` (lines 40+) still registers `@cli.group(name="jules")` with `jules_group.add_command(sync, ...)` and imports `dev_tools.services.jules`.
   * *Gap*: Issue 1 required hard-deleting all legacy Jules interfaces with zero shims.
   * *Resolution*: Included in Issue 9 cleanup scope.

3. **`remote-server` `skills.py` is Primitive & Not Wired to Nodes**:
   * *Observed State*: `remote-server/deep-agent/skills.py` was merged with a hardcoded `INTENT_SKILLS = {"review": ("repo-review",), "implement": ("dispatch-brief",), "fix": ("dispatch-brief",)}` dictionary.
   * *Gap*: It does not parse YAML frontmatter, lacks progressive disclosure summaries, does not support node-targeted skills (`write_spec_issue`, `coder_root_cause_analysis`, `architectural_audit_rubric`, `co_browsing_state_takeover`), and is not injected into `issue_generator.py`, `coder.py`, or `auditor.py`.
   * *Resolution*: Issue 10 upgrades this into the full `SkillsRegistry` and wires it to each LangGraph stage.

4. **`AuditorGraph` `ux_audit_node` Has No Visual Input**:
   * *Observed State*: `remote-server/deep-agent/auditor.py` executes `ux_audit_node` by calling `audit_once()`, which creates an Ollama LLM agent armed only with text search tools (`search_docs`, `read_repo_file`, `grep_repo`).
   * *Gap*: It receives no visual DOM or screenshot metrics because `td audit-impact` does not provide them.
   * *Resolution*: Issues 9 and 10 wire `visual_diff` metrics into the semantic audit rubric.

5. **Local Package Re-Installation Required Post-Merge**:
   * *Observed State*: When `boomtick` is merged, local Python virtualenvs require `pip install -e /home/ari/boomtick/cli` to update the entrypoint scripts; otherwise `td worktree` commands fail with `No such command 'worktree'`.
   * *Resolution*: Documented in verification runbook (Issue 12).

---

## Baseline Architecture Assumptions
With Issues 1–7 merged, the following architectural baseline is now live:
1. **`boomtick`** is the single deterministic tooling engine (`td <subcommand> --json`).
2. **`remote-server`** is the cognitive orchestrator driving the 4-stage LangGraph loop (`Init` -> `IssueGeneratorGraph` -> `InitCoderGraph` -> `AuditorGraph`).
3. **`core_cli/` is completely deleted** (`git rm -rf core_cli`).
4. **Worktree isolation** is enforced at `.worktrees/issue-<id>`.

---

## Issue 8: `[remote-server] Containerize browserless/chromium and Wire Ingress Routing`

````markdown
# [remote-server] Containerize browserless/chromium and Wire Ingress Routing

## Problem Statement
`remote-server` currently routes public traffic through `oauth2-proxy` to `caddy:8080`, directing `/` to `terminal` (ttyd+zellij) and `/buttons*` to `buttons:8000`. To support visual co-browsing and automated UI audits, we need a persistent, containerized browser runtime (`browserless/chromium`) that:
1. Provides an interactive web debugger UI (`DEMO_MODE=true`) for human visual monitoring and takeover.
2. Exposes a WebSocket/CDP endpoint for automated agents.
3. Is securely routed behind `oauth2-proxy` for human web access, while allowing direct Docker-network and Tailscale-mesh CDP access for local and remote CLI agents (`agy`, `td`).

## Objective
Add `browserless` to `remote-server/docker-compose.yml`, configure internal reverse-proxy routing in `caddy/Caddyfile`, configure memory and session limits, and expose the CDP port cleanly for agents.

## Scope & File Boundaries

### Allowed Files (Modify)
- `docker-compose.yml`
- `caddy/Caddyfile`
- `config/oauth2-proxy.cfg`
- `.env.example`
- `README.md`
- `docs/browser-setup.md` (CREATE)

### Forbidden Files
- `terminal/**`
- `buttons/**`

## Implementation Plan

1. **Add `browserless` Service to `docker-compose.yml`:**
   - Image: `ghcr.io/browserless/chromium:latest`
   - Memory allocation: Set `shm_size: '2gb'` to prevent Chromium memory starvation and SIGBUS crashes during concurrent DOM/snapshot rendering.
   - Environment variables:
     - `DEMO_MODE=true` (enables live interactive visual debugger interface)
     - `PREBOOT_CHROME=true` (keeps Chromium warmed up in RAM for sub-second agent connects)
     - `MAX_CONCURRENT_SESSIONS=5`
     - `CONNECTION_TIMEOUT=300000` (5 minutes)
     - `TOKEN=${BROWSERLESS_TOKEN:-}` (secures direct CDP endpoint access)
   - Network & Ports:
     - Expose port `3000` internally on the compose network (`caddy` and `terminal` can reach `http://browserless:3000`).
     - Publish port `127.0.0.1:3000:3000` (and on the Tailscale interface) so `agy` running on a laptop can connect over the secure mesh.

2. **Update `caddy/Caddyfile` for `/browser*` Ingress:**
   - Configure a dedicated subpath route in `caddy/Caddyfile`:
     ```caddy
     :8080 {
         handle /buttons* {
             reverse_proxy buttons:8000
         }

         handle /browser* {
             uri strip_prefix /browser
             reverse_proxy browserless:3000 {
                 # Vital for WebSocket and CDP screencast streaming
                 transport http {
                     dial_timeout 10s
                 }
             }
         }

         handle {
             reverse_proxy terminal:7681
         }
     }
     ```

3. **Configure `oauth2-proxy` Gate:**
   - The existing `oauth2-proxy` configuration terminates TLS and validates GitHub OAuth credentials. Since `/browser*` is routed behind `caddy:8080`, human access via `https://${TERMINAL_DOMAIN}:${TERMINAL_PORT}/browser/` is automatically gated by GitHub OAuth.
   - Internal CLI agents (`agy` running inside the `terminal` container or `remote-server` backend) connect directly to `http://browserless:3000` over the Docker network, completely bypassing OAuth without custom header hacks.
   - Laptop CLI agents connect securely via the Tailscale IP (`ws://s5.tailnet:3000?token=...`).

4. **Document Configuration (`docs/browser-setup.md`):**
   - Provide exact steps for spinning up `browserless`, validating the `/browser/` UI endpoint, and checking CDP reachability.

## Acceptance Criteria
- [ ] `docker compose up -d browserless` starts cleanly with `shm_size: '2gb'`.
- [ ] Navigating to `https://${TERMINAL_DOMAIN}:${TERMINAL_PORT}/browser/` after GitHub OAuth renders the Browserless interactive debugger UI.
- [ ] Direct CDP connections to `http://browserless:3000` from within the `terminal` container succeed without authentication redirects.
- [ ] Chromium process inside `browserless` recovers automatically without container restarts.

## Verification Commands
```bash
docker compose ps browserless
curl -fsS http://127.0.0.1:3000/pressure
docker compose exec terminal node -e "const ws = new (require('ws'))('ws://browserless:3000'); ws.on('open', () => { console.log('CDP OK'); ws.close(); });"
```
````

---

## Issue 9: `[boomtick] Standardize Remote Browserless CDP Driver for td audit-impact & Visual Diffs`

````markdown
# [boomtick] Standardize Remote Browserless CDP Driver for td audit-impact & Visual Diffs

## Problem Statement
In Issue 6 of the consolidation spec, `td audit-impact --json` was established to compute dependency blast radius and trigger visual/DOM diff scripts (`scripts/check-visual-changes.ts`, `scripts/impact-dom-diff.ts`, `ux_report.py`). Currently, these scripts either attempt to spawn local headless Chrome processes or lack a unified CDP connection abstraction. When running inside Docker containers or headless servers, spawning local Chrome causes missing dependency crashes (libgbm, libnss) and high memory spikes.

## Objective
Refactor `boomtick`'s visual and DOM audit engines to connect to a remote `browserless` container via CDP, standardize the driver in `cli/dev_tools/services/browser_cdp.py` and `mcp/src/lib/cdp_client.ts`, and integrate it directly into `td audit-impact --json`.

## Scope & File Boundaries

### Allowed Files (Create / Modify)
- `cli/dev_tools/cli.py`
- `cli/dev_tools/services/browser_cdp.py` (CREATE)
- `cli/dev_tools/services/visual_diff.py`
- `cli/dev_tools/ux_report.py`
- `mcp/src/lib/cdp_client.ts` (CREATE)
- `scripts/check-visual-changes.ts`
- `scripts/impact-dom-diff.ts`
- `cli/tests/test_browser_cdp.py` (CREATE)
- `cli/tests/test_visual_audit.py`

### Forbidden Files
- `mcp/src/tools/worktree.*.ts`
- `cli/dev_tools/services/spec_issue.py`

## Implementation Plan

1. **Implement Unified Python CDP Client (`cli/dev_tools/services/browser_cdp.py`):**
   - Read connection parameters from environment:
     - `BROWSERLESS_ENDPOINT` (default: `http://localhost:3000` or `http://browserless:3000`)
     - `BROWSERLESS_TOKEN` (optional auth token)
   - Expose helper functions:
     - `capture_viewport_snapshot(url: str, output_path: Path, viewport: dict = {"width": 1280, "height": 800}) -> Path`
     - `extract_dom_tree(url: str) -> dict`
     - `compute_visual_regression(baseline_png: Path, candidate_png: Path, diff_png: Path) -> dict`
   - Use `playwright` or direct `websockets`/CDP RPC without launching local browser binaries (`connect_over_cdp`).

2. **Redirect All Internal Logs to `stderr`:**
   - Enforce the "Dirty Stdout" safeguard: all CDP connection logs, debug statements, and screenshot progress messages must be written to `sys.stderr`.
   - `sys.stdout` must strictly output pure JSON when `--json` is passed.

3. **Integrate into `td audit-impact --json` (`cli/dev_tools/cli.py`):**
   - When changed files include UI templates/styles (`*.tsx`, `*.jsx`, `*.vue`, `*.html`, `*.css`), `td audit-impact`:
     1. Checks if `BROWSERLESS_ENDPOINT` is reachable. If unreachable and UI files changed, returns `{"passed": false, "error": "BROWSERLESS_UNREACHABLE"}`.
     2. Spins up local Vite/dev server preview if configured in `project_config.json`.
     3. Captures DOM snapshot and viewport screenshot of changed routes.
     4. Compares against baseline snapshots from `origin/main`.
     5. Attaches `{"visual_diff": {"mismatch_percentage": float, "dom_mutations": int, "diff_image_path": str}}` to the JSON payload.

4. **Write Unit and Integration Tests (`cli/tests/test_browser_cdp.py`):**
   - Mock CDP server responses to verify payload extraction and diff calculation without requiring a live browser container during CI.
   - Integration test running against live `browserless` if `BROWSERLESS_ENDPOINT` is provided.

## Acceptance Criteria
- [ ] `td audit-impact --worktree <path> --json` executes visual and DOM audits without launching local Chrome binaries.
- [ ] Stdout contains exclusively valid JSON; all debug and progress telemetry is isolated to stderr.
- [ ] Snapshot and DOM extraction handle timeouts gracefully with descriptive error payloads.
- [ ] Unit tests pass in `cli/tests/test_browser_cdp.py` and `cli/tests/test_visual_audit.py`.

## Verification Commands
```bash
pytest cli/tests/test_browser_cdp.py cli/tests/test_visual_audit.py
python -m dev_tools.cli audit-impact --worktree . --json > /tmp/impact.json 2> /tmp/impact.log
jq . /tmp/impact.json
```
````

---

## Issue 10: `[remote-server] Dynamic Skills Framework & Progressive Disclosure Engine for LangGraph Agents`

````markdown
# [remote-server] Dynamic Skills Framework & Progressive Disclosure Engine for LangGraph Agents

## Problem Statement
In Issues 4, 5, and 6, LangGraph deep agents (`IssueGeneratorGraph`, `InitCoderGraph`, `AuditorGraph`) were established to drive autonomous coding cycles. Currently, domain guidelines (anti-slop constraints, root cause tracing, semantic audit rubrics, and co-browsing interaction rules) are either embedded directly into Python string literals or passed in full to LLM system prompts. This wastes context tokens on every turn and lacks a clean runbook format that engineers can update without modifying graph code.

## Objective
Build a dynamic **Skills Framework** in `remote-server/deep-agent/` with progressive disclosure: maintain modular Markdown skills in `deep-agent/skills/`, index lightweight 100-token summaries at startup, and load deep execution instructions dynamically into node prompts only when active.

## Scope & File Boundaries

### Allowed Files (Create / Modify)
- `deep-agent/skills_registry.py` (CREATE)
- `deep-agent/skills/write_spec_issue.md` (CREATE)
- `deep-agent/skills/coder_root_cause_analysis.md` (CREATE)
- `deep-agent/skills/architectural_audit_rubric.md` (CREATE)
- `deep-agent/skills/co_browsing_state_takeover.md` (CREATE)
- `deep-agent/issue_generator.py`
- `deep-agent/coder.py`
- `deep-agent/auditor.py`
- `tests/test_skills_registry.py` (CREATE)

### Forbidden Files
- `frontend/**`
- `caddy/**`

## Implementation Plan

1. **Implement `SkillsRegistry` (`deep-agent/skills_registry.py`):**
   - Scans `deep-agent/skills/*.md` for YAML frontmatter (`name`, `description`, `trigger_nodes`, `dependencies`).
   - Generates a compact index summary (~100 tokens per skill) for inclusion in agent routing prompts.
   - Provides `load_skill(name: str) -> str` that reads the full markdown file and caches it in memory.
   - Provides `inject_skills_for_node(node_name: str, base_system_prompt: str) -> str` that appends only skills targeted for that graph node.

2. **Author the 4 Core Deep-Agent Skills (Complete Verbatim Definitions):**

   ### `deep-agent/skills/write_spec_issue.md`
   ````markdown
   ---
   name: write_spec_issue
   description: Enforces anti-slop architectural style, deterministic structure, file:line citations, and strict acceptance criteria for Issue Specs.
   trigger_nodes:
     - draft_spec_node
   ---
   # Skill: Design Specification Authoring & Anti-Slop Guide

   ## 1. Cognitive Guardrails & Anti-Slop Style
   - **No Vague Passive Directives**: Never write "the system should be fast" or "errors should be handled appropriately". You MUST write explicit, measurable thresholds (e.g. "timeout must be bounded to 5000ms", "must return HTTP 422 with JSON `{error: 'INVALID_SPEC'}`").
   - **No Speculative Abstractions**: Never create generic adapter patterns, wrapper facades, or placeholder interfaces for "future extensibility". Implement only the minimal concrete mechanism required.
   - **Zero Tolerance for Placeholders**: Any appearance of `TODO`, `TBD`, `FIXME`, or `...` will cause immediate rejection by `td validate-issue --json`.

   ## 2. Mandatory Structural Sections
   Every spec issue generated must contain these exact Markdown headers:
   1. `## Problem Statement`: Must cite exact existing file paths and line numbers (`file.py#L12-L34`) identified during investigation.
   2. `## Objective`: Single paragraph describing the target functional state.
   3. `## Scope & File Boundaries`:
      - `### Allowed Files (Create / Modify / Delete)`: Explicit list of file paths. No wildcards in write scopes.
      - `### Forbidden Files`: Explicit list of critical infrastructure or out-of-scope files.
   4. `## Implementation Plan`: Numbered step-by-step technical plan.
   5. `## Acceptance Criteria`: Unchecked markdown checklist (`- [ ]`) verifying every required functional behavior.
   6. `## Verification Commands`: A single fenced bash code block containing non-empty, executable test and lint commands that exit with code 0 on success.

   ## 3. Repair Protocol on Validation Failure
   When `td validate-issue` returns `valid == false`:
   - Inspect `validation_result["errors"]`.
   - If an allowed file does not exist on disk and is not marked `(CREATE)`, correct the path using findings from `codebase_investigator_node`.
   - If verification commands are missing or non-executable, look up the repository's `project_config.json` test scripts and supply the exact command.
   ````

   ### `deep-agent/skills/coder_root_cause_analysis.md`
   ````markdown
   ---
   name: coder_root_cause_analysis
   description: Enforces systematic root-cause tracing and hypothesis validation during coder re-planning loops to prevent hallucination cycles.
   trigger_nodes:
     - validated_replanner_node
   ---
   # Skill: Coder Root Cause Analysis & Replanning Runbook

   ## 1. Anti-Hallucination Re-Planning Protocol
   When deterministic gates (`td verify-scope`, `td audit-code`, or verification commands) fail consecutively:
   - **DO NOT** attempt immediate shallow edits.
   - **DO NOT** wrap failing assertions in `try/except` or add type suppressions (`# type: ignore`, `// @ts-ignore`).
   - You MUST execute the following 4-step diagnostic loop before producing revised code.

   ## 2. Four-Step Diagnostic Loop
   1. **Stack Trace Deconstruction**:
      - Read the exact failing line and assertion message from test stdout/stderr.
      - Locate the caller and callee frames inside the active worktree.
   2. **Component Isolation & Dependency Inspection**:
      - Query `td rag query --query "<failing symbol or module>" --json` to inspect the authoritative contracts of all interacting components.
      - Verify whether the failure is caused by an unfulfilled interface assumption or a broken import.
   3. **Explicit Hypothesis Formulation**:
      - Write an explicit one-sentence hypothesis in the persistent decision ledger (`decisions.py`):
        `"Failure occurred because [Root Cause]; fix requires modifying [Component] to [Action]."`
   4. **Scope-Preserving Plan Adjustment**:
      - Ensure the repair does not add files outside `Allowed Files`. If an unlisted file genuinely needs modification, invoke `td validate-issue` to verify the amended scope before proceeding.
   ````

   ### `deep-agent/skills/architectural_audit_rubric.md`
   ````markdown
   ---
   name: architectural_audit_rubric
   description: Deep semantic audit checklist evaluating acceptance criteria traceability, anti-slop compliance, tautological tests, and downstream impact.
   trigger_nodes:
     - deep_semantic_auditor_node
   ---
   # Skill: Architectural Semantic Audit Rubric

   ## 1. Core Evaluation Tenets
   The deep semantic auditor only runs after zero-token deterministic gates (`td verify-scope` and unit tests) pass 100%. Your role is cognitive verification that code cannot easily check automatically.

   ## 2. Audit Verification Checklist
   1. **1-to-1 Acceptance Criteria Traceability**:
      - Review every checklist item in the original Issue Spec.
      - Map each item to concrete `file:line` diff evidence in the worktree.
      - If any criterion lacks explicit implementation evidence, mark `verified: false` and reject with `CHANGES_REQUESTED`.
   2. **Tautological Test Detection**:
      - Scrutinize all new or modified test suites.
      - Reject tests that only verify mocks (e.g. mocking a function and asserting that the mock returned its configured return value). Tests MUST exercise real integration or logic boundaries.
   3. **Dead Abstractions & Leaky Interfaces**:
      - Verify that no unused parameters, unused exports, or dead helper functions were left behind.
   4. **Downstream Blast-Radius Verification**:
      - Read `td audit-impact --json`'s `downstream_dependents`.
      - Confirm that changed function signatures or schemas did not break un-migrated downstream callers.
   5. **Visual & DOM Consistency (if `ui_changed == true`)**:
      - Inspect `visual_diff` metrics. Ensure layout shift, DOM node deletion, or CSS regressions are intended by the spec.
   ````

   ### `deep-agent/skills/co_browsing_state_takeover.md`
   ````markdown
   ---
   name: co_browsing_state_takeover
   description: Protocol governing human-in-the-loop co-browsing, immediate takeover handling, command buffer suppression, and post-takeover DOM re-indexing.
   trigger_nodes:
     - visual_interaction_node
     - td_impact_and_visual_node
   ---
   # Skill: Co-Browsing Interaction & Immediate Takeover Protocol

   ## 1. Visual Pre-Verification Tenet
   - Before executing any programmatic DOM click, form fill, or navigation action:
     - Take a snapshot and verify target element coordinates and visibility.
     - Never blindly send coordinates from an outdated turn.

   ## 2. Immediate Human Takeover Protocol
   - The user has priority authority over the shared browser head at all times.
   - When an incoming `POST /api/agent/pause` event is triggered by user interaction (pointerdown/keydown):
     1. **Drop In-Flight Buffer**: Discard any scheduled or queued browser commands in the current step immediately.
     2. **State Transition**: Set `agent_execution_state = "PAUSED_BY_USER"` in the persistent ledger.
     3. **Halt Loop**: Yield execution without throwing an error. Notify the UI: *"Manual takeover active. Agent standing by."*
     4. **No Mutation While Paused**: Do not issue any CDP or DOM commands until an explicit resume signal is received.

   ## 3. Mandatory Post-Takeover Resumption Protocol
   - When `POST /api/agent/resume` is received:
     1. **DO NOT** reuse prior element references, CSS selectors, or snapshot IDs (`e1`, `e2`, etc.) — the user may have navigated, filled forms, or opened new tabs.
     2. **Mandatory Re-Snapshot**: Immediately capture a fresh DOM footprint (`snapshot -i` or `td audit-impact --json`).
     3. **Re-Plan From Fresh State**: Re-evaluate the active task against the updated page state before resuming automated execution.
   ````

3. **Integrate Skills into LangGraph Agent Nodes:**
   - In `issue_generator.py`: `draft_spec_node` injects `write_spec_issue`.
   - In `coder.py`: `validated_replanner_node` injects `coder_root_cause_analysis`.
   - In `auditor.py`: `deep_semantic_auditor_node` injects `architectural_audit_rubric`.

4. **Write Verification Tests (`tests/test_skills_registry.py`):**
   - Verify frontmatter parsing and summary generation.
   - Verify that loading non-existent skills raises `SkillNotFoundError`.
   - Verify that node prompt injection injects only relevant skills without bloating system prompts.

## Acceptance Criteria
- [ ] All 4 core skills exist as well-formed markdown files with YAML frontmatter.
- [ ] `SkillsRegistry` indexes skills progressively and loads full content on demand.
- [ ] LangGraph nodes dynamically incorporate targeted skills.
- [ ] Tests in `tests/test_skills_registry.py` pass.

## Verification Commands
```bash
pytest tests/test_skills_registry.py tests/test_issue_generator.py tests/test_coder.py tests/test_auditor.py
```
````

---

## Issue 11: `[remote-server] Co-Browsing Frontend Panel with Immediate Takeover & State Synchronization`

````markdown
# [remote-server] Co-Browsing Frontend Panel with Immediate Takeover & State Synchronization

## Problem Statement
The `remote-server` web interface currently provides the terminal view (`/`) and action buttons (`/buttons`). While an agent is navigating pages or performing visual regressions, the user needs an embedded, real-time visual panel where they can watch the live browser stream and immediately take over mouse/keyboard control if needed, without causing race conditions or freezing the LangGraph agent state.

## Objective
Embed the `browserless` interactive debugger into the `remote-server` web interface, implement an immediate-takeover event interception hook on the canvas, provide pause/resume state APIs in the backend, and enforce the fresh DOM re-index protocol upon resumption.

## Scope & File Boundaries

### Allowed Files (Create / Modify)
- `buttons/server.py`
- `buttons/templates/index.html` (or frontend view components)
- `buttons/static/co_browsing.js` (CREATE)
- `buttons/static/co_browsing.css` (CREATE)
- `backend/api/routers/agent_control.py` (CREATE)
- `deep-agent/pipeline.py`
- `tests/test_co_browsing_api.py` (CREATE)

### Forbidden Files
- `terminal/Dockerfile`
- `caddy/Caddyfile`

## Implementation Plan

1. **Implement Agent Control API (`backend/api/routers/agent_control.py`):**
   - Expose endpoints:
     - `POST /api/agent/pause` -> sets `agent_execution_state = "PAUSED_BY_USER"` in shared state/ledger, signals current running step to halt subsequent mutations.
     - `POST /api/agent/resume` -> sets `agent_execution_state = "RUNNING"`, dispatches refresh signal to agent graph to capture fresh DOM snapshot before executing next action.
     - `GET /api/agent/state` -> returns `{"state": "RUNNING" | "PAUSED_BY_USER" | "IDLE", "active_issue": int | None, "current_step": str}`.

2. **Embed Visual Canvas in Web Dashboard (`buttons/templates/index.html`):**
   - Add a collapsible Co-Browsing Drawer / Panel containing:
     - An `<iframe>` pointing to `/browser/` (proxied to `browserless:3000`).
     - A floating status overlay indicating current agent state (`Active`, `Paused by User`, `Synchronizing`).
     - A persistent **[Resume Agent]** button that appears whenever manual takeover is triggered.

3. **Build Immediate-Takeover Event Listener (`buttons/static/co_browsing.js`):**
   - Wrap the iframe container with pointer and keyboard listeners.
   - On first `pointerdown` or `keydown` inside the browser view when `agent_execution_state === "RUNNING"`:
     1. Dispatches an immediate asynchronous `POST /api/agent/pause`.
     2. Updates floating UI banner: *"Manual Control Active — Agent Paused"*.
     3. Reveals the **[Resume Agent]** button.
   - When user clicks **[Resume Agent]**:
     1. Dispatches `POST /api/agent/resume`.
     2. Sets banner to *"Synchronizing DOM..."*.
     3. Agent takes a fresh viewport snapshot, re-indexes element references, and resumes autonomous execution.

4. **Update LangGraph Pipeline Loop (`deep-agent/pipeline.py`):**
   - Check `agent_execution_state` before executing any browser mutation step.
   - If `PAUSED_BY_USER`, enter a wait condition until `RESUMED` signal is received.
   - On resume, invoke `skills/co_browsing_state_takeover.md` runbook to refresh page state before proceeding.

5. **Write Verification Tests (`tests/test_co_browsing_api.py`):**
   - Verify `POST /api/agent/pause` transitions state to `PAUSED_BY_USER`.
   - Verify pipeline respects pause signal and does not fire subsequent page mutations while paused.
   - Verify `POST /api/agent/resume` triggers DOM re-snapshot and clears pause lock.

## Acceptance Criteria
- [ ] Embedded `/browser/` iframe renders inside the web interface without iframe security blocks (`X-Frame-Options` configured cleanly).
- [ ] Clicking or typing inside the browser canvas instantly pauses the autonomous agent and displays the Resume button.
- [ ] Resuming the agent triggers a fresh DOM snapshot before executing new actions.
- [ ] Tests in `tests/test_co_browsing_api.py` pass.

## Verification Commands
```bash
pytest tests/test_co_browsing_api.py
curl -fsS -X POST http://127.0.0.1:8000/api/agent/pause
curl -fsS http://127.0.0.1:8000/api/agent/state | jq .
curl -fsS -X POST http://127.0.0.1:8000/api/agent/resume
```
````

---

## Issue 12: `[boomtick + remote-server] agy CLI Preflight Verification & Autonomous Co-Browsing Test Suite`

````markdown
# [boomtick + remote-server] agy CLI Preflight Verification & Autonomous Co-Browsing Test Suite

## Problem Statement
Before enabling full autonomous end-to-end LangGraph runs with co-browsing, we need an automated preflight test harness driven by the **`agy`** CLI agent. This verifies that:
1. `browserless` CDP connectivity works end-to-end through the network layers.
2. Immediate user takeover pauses automated commands cleanly.
3. `td audit-impact` correctly extracts visual diffs from the container and attaches them to `AuditorGraph`.

## Objective
Create an end-to-end test script and verification runbook (`scripts/verify-co-browsing-preflight.py` and `tests/test_co_browsing_e2e.py`) that can be executed directly by `agy` to validate the entire co-browsing and skills integration.

## Scope & File Boundaries

### Allowed Files (Create)
- `scripts/verify-co-browsing-preflight.py` (CREATE)
- `tests/test_co_browsing_e2e.py` (CREATE)
- `docs/co-browsing-runbook.md` (CREATE)

### Forbidden Files
- `docker-compose.yml`
- `caddy/Caddyfile`

## Implementation Plan

1. **Build `verify-co-browsing-preflight.py`:**
   - Verifies:
     1. HTTP reachability of `http://browserless:3000/pressure`.
     2. CDP WebSocket connection and navigation (`p.chromium.connect_over_cdp`).
     3. Captures a test screenshot and asserts image byte integrity.
     4. Simulates a `POST /api/agent/pause` call; asserts CDP commands halt.
     5. Simulates `POST /api/agent/resume`; asserts fresh DOM snapshot is taken.
     6. Invokes `td audit-impact --worktree . --json` and asserts `visual_diff` object exists in stdout.

2. **Create Pytest Harness (`tests/test_co_browsing_e2e.py`):**
   - Integrates preflight steps into the standard CI/CD and local test suite.

3. **Document Execution via `agy` (`docs/co-browsing-runbook.md`):**
   - Step-by-step instructions for human developers or `agy` CLI sessions to run the preflight check and audit logs.

## Acceptance Criteria
- [ ] `scripts/verify-co-browsing-preflight.py` executes all 6 verification stages successfully.
- [ ] Visual diff metrics and screenshots are generated without launching local browser binaries.
- [ ] Immediate takeover and resume loop verified end-to-end.
- [ ] Test suite passes with exit code 0.

## Verification Commands
```bash
python scripts/verify-co-browsing-preflight.py
pytest tests/test_co_browsing_e2e.py
```
````
