### 1. Tool Audit (`boomtick` MCP & `td` CLI)

You have already purged 11 redundant MCP tools (in `boomtick #1126`), leaving 17 active tools in `mcp/src/tools/`. However, your current architecture still mixes **interactive LLM tools** (MCP) with **deterministic pipeline orchestration** (`td` CLI). Deep agents work best when their tool surface is tiny and high-leverage, while deterministic gates execute automatically inside your state graph nodes rather than relying on the LLM to call them.

#### MCP Tool Matrix (`boomtick/mcp/src/tools/`)

| Tool Name | Verdict | Rationale |
| --- | --- | --- |
| `td.init_and_rag`

 | **KEEP (Split)** | Essential for querying the vector store (`vector_store.py`), but decouple `init` (git pull/build) from `rag` search so the Issue Generator isn't re-running builds on every query.

 |
| `td.validate_issue`

 | **KEEP** | Load-bearing for the Issue Generator agent's self-correction loop (`spec_issue.py`).

 |
| `td.verify_scope`

 | **KEEP** | Load-bearing for the Coder agent to deterministically check touched files against the issue's allowed scope (`scope_gate.py`).

 |
| `td.audit_worktree`

 | **KEEP** | Primary composite gate for the Auditor and Coder (`ast_audit.py` + `scope_gate.py`).

 |
| `repo.read_agent_context`

 | **KEEP** | Fast, pre-compiled architectural context (`context_builder.py`).

 |
| `repo.run_tests`

 | **KEEP** | Scoped test runner for the Coder loop.

 |
| `github.create_issue` / `issue_update` / `issue_view`

 | **KEEP** | Core CRUD for the Issue Generator and Coder to read/write living spec plans.

 |
| `agent.submit_pr`

 | **DEMOTE TO CLI** | PR submission (`submit_pr.py`) should be a deterministic exit node in the graph once gates pass, not an LLM tool choice.

 |
| `worktree.create` / `list` / `remove`

 | **REMOVE FROM MCP** | Agents should *never* manage their own worktree lifecycle. The orchestrator (`remote-server` or `td` runner) should spawn the worktree and jail the agent's `cwd` inside it. |
| `github.get_pr` / `issue_comment` / `repo.read_ci_logs`

 | **PRUNE / DEMOTE** | Only needed for reactive CI-repair bots. Remove from the primary Issue/Coder/Auditor agent toolsets to save context tokens. |

#### `td` CLI Subcommands (`boomtick/cli/dev_tools/`)

* **Keep as Deterministic Graph Nodes:** `td init`, `td worktree`, `td verify-scope`, `td audit-code`, `td audit-impact`, `td validate-issue`.


* **Keep for Agent Discovery:** `td rag`, `td context-warm`.


* **Consolidate/Remove:** `td session` (`local_sessions.py` overlaps with `mcp/src/lib/session_manager.ts` and `remote-server` job tracking).



---

### 2. Separation of Concerns: `boomtick` vs. `remote-server`

Currently, both repositories contain overlapping agent definitions, session managers, and AST/slop auditors (`boomtick/.agents/scripts/audit-ai-slop.py` vs. `remote-server/.agents/skills/audit-impeccable-slop/`, and duplicate CDP/session handlers).

* **`boomtick` (The Deterministic Engine & Tooling Layer):**
* Owns the `td` Python CLI (`cli/dev_tools/`), deterministic AST/scope/visual-diff gates (`scripts/impact-*.ts`, `services/ast_audit.py`, `services/scope_gate.py`), Chroma/SQLite vector indexing (`services/vector_store.py`), and the MCP server (`mcp/`).


* **Rule:** `boomtick` should be completely stateless regarding long-running jobs or UI. It takes a repo path/worktree, runs deterministic checks or MCP tools, and returns structured JSON.


* **`remote-server` (The Control Plane, UI & Stateful Orchestrator):**
* Owns the operator console (Now, Chat, Briefing, Jobs), persistent job state/queues, human-in-the-loop (HITL) review approvals, live browser streaming UI, and triggering `td` / LangGraph workflows.
* **Rule:** Delete duplicated Python/TS validation scripts from `remote-server` if they aren't load-bearing importers, or thin-wrap them so `remote-server` invokes `td` subcommands via subprocess/MCP. Move `.claude/skills/impeccable` and `.agents/skills/lane-*` into a single canonical skill registry read by `boomtick/mcp/src/lib/skills.ts`.





---

### 3. Deep Agent Workflows (LangGraph Architecture)

Your custom DAG in `boomtick/cli/dev_tools/workflows/` (`graph.py`, `node.py`, `nodes.py`, `runner.py`) is already structured like a state machine. Migrating or wrapping `nodes.py` into **LangGraph** gives you native SQLite/Postgres checkpointing (`MemorySaver` / `SqliteSaver`), `interrupt_before` for human sign-off on specs, and strict conditional edges where deterministic Python functions gate LLM transitions.

#### Phase 0: `Init` (Deterministic Pre-Flight Node — Zero LLM Tokens)

* **Existing Components:** `scripts/hooks/post-merge`, `cli/dev_tools/services/vector_store.py`, `mcp/src/lib/dependency_cache.ts`, `cli/dev_tools/resources/build-repo-context.py`.


* **How it Works & Gaps to Fix:**
* **Git Sync:** Add a `git fetch origin main && git update-ref refs/heads/main origin/main` step in `td init` so local `main` is always fast-forwarded before branching, even without checking it out.
* **Vector DB & Context:** Hook `post-merge` and `post-checkout` to run incremental `vector_store.py` updates (hashing file mtimes/blobs so only changed files re-embed) and refresh `build-repo-context.py`.


* **Warm Dependencies:** Use `pnpm fetch` and shared `pnpm` content-addressable stores so worktrees link `node_modules` in milliseconds instead of running fresh network installs.



#### Agent 1: `Issue Generator` (Spec & PRD Author)

* **Existing Components:** `.agents/workflows/issue-authoring.md`, `cli/dev_tools/services/spec_issue.py`, `td.validate_issue`.


* **LangGraph Flow:**
1. **Node `init_context`:** Deterministic call to `td context-warm` + `td rag` based on user prompt.
2. **Node `explore_codebase`:** Scoped LLM agent with read-only tools (`td.init_and_rag`, `repo.read_agent_context`, file read/grep). If intent is ambiguous, hits a LangGraph `interrupt` to ask the user clarifying questions in `remote-server` Chat.


3. **Node `draft_spec`:** Generates the structured PRD (Target Files Allowlist, Forbidden Files, Acceptance Criteria, Verification Commands) using `plan-template.md`.


4. **Edge `validate_spec_gate` (Deterministic Loop):** Runs `spec_issue.py` (`td validate-issue`) to verify that all listed file paths exist, schemas match, and verification commands are valid. If invalid, routes back to `draft_spec` with exact validation errors (max 3 retries). Once valid, posts/updates the GitHub Issue.





#### Agent 2: `Init Coder` (Worktree Execution Loop)

* **Existing Components:** `cli/dev_tools/services/worktrees.py`, `remote-server/.agents/skills/lane-coder/SKILL.md`, `cli/dev_tools/services/scope_gate.py`, `cli/dev_tools/services/repair_service.py`.


* **LangGraph Flow:**
1. **Node `provision_worktree` (Deterministic):** Calls `worktrees.py` to create `.worktrees/issue-<id>` off updated `main`, links cached dependencies, and locks the agent's filesystem root to that worktree path.


2. **Node `code_step` (LLM):** Writes code and unit tests strictly following the validated Issue Spec.
3. **Node `deterministic_gate` (Zero LLM):** Runs sequentially:
* `scope_gate.py` (`td verify-scope`) — immediately fails if the agent touched files outside the Issue's allowlist.


* Fast static checks: `oxlint`, `tsc --noEmit`, `pylint`.


* Scoped unit tests (`repo.run_tests`).




4. **Conditional Edge:**
* If `scope_gate` fails $\rightarrow$ auto-revert unauthorized files (`git checkout -- <file>`) and feed error back to `code_step`.
* If tests/lint fail $\rightarrow$ route error trace back to `code_step`.
* **Scope Escalation (Plan Update):** If the Coder discovers a necessary file is missing from the allowlist, it cannot edit it directly; it must emit a `RequestSpecUpdate` state transition, which routes back to the `Issue Generator` validator to approve and update the allowlist before coding resumes.





#### Agent 3: `Issue + Code Auditor` (Gated Deep Review)

* **Existing Components:** `cli/dev_tools/services/ast_audit.py`, `.agents/scripts/audit-ai-slop.py`, `scripts/detect-antipatterns.mjs`, `scripts/detect-semantic-duplicates.mjs`, `scripts/impact-analysis.ts`, `scripts/impact-visual-diff.ts`, `lib/codeReviewOrchestrator.ts`.


* **LangGraph Flow:**
1. **Node `hard_gate_barrier` (Deterministic):** The Auditor *cannot* be invoked until `td audit-code` passes 100%:
* `ast_audit.py` & `audit-ai-slop.py` (blocks placeholder comments, swallow-exceptions, unused vars).


* `detect-antipatterns.mjs` & `detect-semantic-duplicates.mjs` (`jscpd` / `.fallowrc.json` dead code checks).


* `dependency-cruiser` architecture boundary checks.




2. **Node `impact_and_visual_diff` (Deterministic):** Runs `scripts/impact-analysis.ts`, `impact-dom-diff.ts`, and `impact-visual-diff.ts` against `main` (`impact-build-main.ts`) to produce DOM/pixel diffs and dependency blast-radius graphs.


3. **Node `llm_semantic_audit` (Scoped LLM):** Invokes `codeReviewOrchestrator.ts` + `visualReviewOrchestrator.ts` with the Issue PRD, git diff, and visual diff images. Verifies the code actually solves the user's issue and adheres to `visual_guidelines.json`.


4. **Conditional Edge:** If rejected, writes structured actionable findings back to the graph state and routes to `Init Coder`. If approved, triggers `submit_pr.py`.





---

### 4. Assessing & Fixing the Co-Browsing Feature

Your current co-browsing implementation is split across `boomtick/cli/dev_tools/services/browser_cdp.py` and `boomtick/mcp/src/lib/cdp_client.ts` (plus `start_browsermcp.sh`).

**Why It Doesn't Work Well Currently:**

1. **Dual Competing Clients:** Both Python (`browser_cdp.py`) and TypeScript (`cdp_client.ts`) attach to the Chrome DevTools Protocol target independently. When both—or the human operator—send `Input.dispatchMouseEvent`, `Input.dispatchKeyEvent`, or `Page.navigate` without a session lock, events interleave, focus is stolen, and text input gets garbled.


2. **Polling Screenshots Instead of Streaming:** Taking discrete `Page.captureScreenshot` snapshots over MCP causes high latency and resolution degradation.
3. **Lack of Shared Autonomy State (Control Lease):** There is no arbitration layer in `remote-server` to pause the agent when the human clicks/types into the viewport.

**How to Fix Co-Browsing:**

* **Single CDP Multiplexer in `remote-server`:** Remove `browser_cdp.py` from `boomtick` and make a single CDP proxy service in `remote-server` own the WebSocket connection to Headless Chrome (`--remote-debugging-port=9222`).
* **Use `Page.startScreencast` for the UI:** Stream frames to the `remote-server` frontend via WebSocket using CDP's native `Page.screencastFrame` (acknowledging frames with `Page.screencastFrameAck`) at a fixed `1280x800` viewport (`Emulation.setDeviceMetricsOverride`).
* **Atomic Text Input:** Replace character-by-character `dispatchKeyEvent` loops with `Input.insertText` (for instant, reliable string injection) after verifying element focus via `DOM.focus`.
* **Shared Autonomy Mutex (Human Override):** Implement a state machine (`AGENT_CONTROL` | `HUMAN_OVERRIDE` | `PAUSED`) in the CDP proxy. The moment the `remote-server` UI detects a human `mousedown` or `keydown` on the canvas, immediately flip the lock to `HUMAN_OVERRIDE`, reject/pause in-flight MCP browser tool calls with a `"Human operator took control"` status, and let the human finish before handing the lease back to the agent.

---

### 5. Concrete Next Steps

1. **Enforce Repo Boundaries:**
* Keep `boomtick` as the pip/npm-installable deterministic engine (`td` CLI + MCP server).


* Have `remote-server` consume `td` CLI commands and MCP tools rather than maintaining duplicate audit skills and CDP clients.


2. **Trim the MCP Surface Area:**
* Remove `worktree.create`, `worktree.list`, `worktree.remove`, and `agent.submit_pr` from `mcp/src/mcp/tools.ts` so the LLM cannot call them directly; invoke their underlying Python/TS services (`worktrees.py`, `submit_pr.py`) deterministically from the workflow runner.




3. **Wire the 4-Stage LangGraph Pipeline in `cli/dev_tools/workflows/`:**
* Update `nodes.py` and `graph.py` to chain `InitNode` $\rightarrow$ `IssueGeneratorGraph` $\rightarrow$ `CoderWorktreeGraph` $\rightarrow$ `DeterministicAuditBarrier` $\rightarrow$ `LLMAuditorNode`, using SQLite checkpointing in `.td/sessions.db` so interrupted runs can resume without token waste.




4. **Fix Co-Browsing Input & Leasing:**
* Consolidate CDP into `cdp_client.ts`, switch text entry to `Input.insertText`, lock the viewport via `Emulation.setDeviceMetricsOverride`, and add a human-override mutex in `remote-server`.




Your current skill architecture is fragmented across four different formats and two repositories, which causes **context bloat, conflicting instructions, and agents having to "discover" rules they should already have**.

---

### 1. Audit of Current Skills & Prompts

Right now, agent instructions live in four competing places:

1. **`remote-server/.agents/skills/` (Active Lane Skills):** `lane-coder`, `lane-orchestrator`, `audit-impeccable-slop`, and `audit-gemini-talker`. Also a broken symlink `.claude/skills/antislop-code` pointing to a missing `../../.agents/skills/antislop-code`.


2. **`remote-server/.claude/skills/impeccable/` (Vendor Design Skill):** Contains `SKILL.md`, 27 reference markdown files (`audit.md`, `critique.md`, `polish.md`, `harden.md`, etc.), 4 sub-agents in `.claude/agents/impeccable-*.md`, and a **vendored binary** (`scripts/bin/linux-x64/impeccable`) committed directly into git.


3. **`boomtick/.agents/workflows/` (Markdown Workflows):** 9 workflow files (`issue-authoring.md`, `issue-audit.md`, `review-pr.md`, `review-ux.md`, `ci-remediation.md`, etc.) plus `INSTRUCTION_LAYERS.md` and `AGENT_CONTRACT.md`.


4. **`boomtick/mcp/src/agents/` (Role Prompts):** 6 prompt templates (`verifier-agent.prompt.md`, `repair-agent.prompt.md`, `repo-context.prompt.md`, `pr-writer.prompt.md`, `conflict-scout.prompt.md`, `pr-consolidation.prompt.md`) alongside `boomtick/mcp/src/lib/skills.ts` and `td skill`.



*(Note: In our Gemini workspace, you also have two custom skills—`get-more-perspectives` and `match-my-writing-style`—which are useful for product/blog ideation, not code execution).*

---

### 2. What’s Hurting Your Deep Agents

* **Pull-Based Skill Discovery Wastes Tokens:** Having agents call an MCP tool (`skills.ts` / `td skill`) to read `lane-coder` or `issue-authoring` mid-task wastes a turn and risks the agent skipping the skill entirely. In a scoped LangGraph setup, **skills should be deterministically injected into the node's system prompt** based on the current stage.


* **Massive Context Bloat from `impeccable`:** `.claude/skills/impeccable/` has 27 reference files (`clarify.md`, `overdrive.md`, `bolder.md`, `quieter.md`, etc.). If an agent loads `impeccable` during a frontend task or UX audit, it floods the context window with thousands of tokens of generic design prose, drowning out your strict `visual_guidelines.json` and `ux-audit.config.json` in `boomtick`.


* **Duplicate "Anti-Slop" & Audit Rules:** You have anti-slop rules in `remote-server/.agents/skills/audit-impeccable-slop/SKILL.md`, `boomtick/.agents/scripts/audit-ai-slop.py`, `boomtick/scripts/detect-antipatterns.mjs`, and `.claude/skills/antislop-code`. When an LLM reads prose instructions about "don't write slop" instead of being gated by a deterministic AST check, it still hallucinates—just with different formatting.


* **Orchestration in Markdown (`lane-orchestrator`):** Having an LLM read `lane-orchestrator/SKILL.md` or `agent-orchestrator.yml` to decide *when* to run `td verify-scope` or create worktrees is fragile. State transitions belong in Python (`graph.py` / LangGraph), not in a markdown skill.



---

### 3. Skill Retention & Purge Matrix

| Skill / Directory | Verdict | Action |
| --- | --- | --- |
| **`lane-orchestrator` (`remote-server`)**

 | **DELETE** | Replace with deterministic LangGraph edges in `boomtick/cli/dev_tools/workflows/graph.py`. Never let an LLM orchestrate worktree creation or gate ordering via prompt instructions.

 |
| **`.claude/skills/antislop-code` (`remote-server`)**

 | **DELETE** | Broken symlink (`../../.agents/skills/antislop-code`) and redundant with `boomtick/.agents/scripts/audit-ai-slop.py`.

 |
| **`audit-impeccable-slop` (`remote-server`)**

 | **CONVERT TO AST + TRIM** | Move any checkable rules into `boomtick/cli/dev_tools/services/ast_audit.py` and `audit-ai-slop.py`. Keep at most a 30-line rubric for the final Auditor LLM node.

 |
| **`.claude/skills/impeccable/` (27 files + binary)**

 | **QUARANTINE / PURGE** | Remove the 15MB `linux-x64/impeccable` binary from git. Distill only the 2–3 files you actually use (`audit.md`, `polish.md`, `harden.md`) into a single compact `ui-craft.md` skill (<150 lines) that only loads when `.tsx`/`.css` files are in the Issue's allowlist.

 |
| **`audit-gemini-talker` (`remote-server`)**

 | **KEEP (Scoped)** | Keep in `remote-server` strictly for auditing conversational/briefing voice prompts, completely isolated from coding agents.

 |
| **`lane-coder` (`remote-server`)**

 | **MOVE TO `boomtick` & SPLIT** | Move to `boomtick` as the canonical system prompt for the **Init Coder** LangGraph node, stripped of git/worktree setup commands (since the `Init` node handles those deterministically). |
| **`boomtick/.agents/workflows/issue-authoring.md`**

 | **KEEP (Node Prompt)** | Bind directly as the system prompt for the **Issue Generator** LangGraph node.

 |
| **`boomtick/.agents/workflows/review-pr.md` & `review-ux.md**`

 | **KEEP (Node Prompt)** | Bind directly to the **Issue + Code Auditor** LangGraph node (only `review-ux.md` loads if frontend files are touched).

 |
| **`boomtick/mcp/src/agents/*.prompt.md`**

 | **MERGE** | Merge `verifier-agent.prompt.md` and `repair-agent.prompt.md` into `cli/dev_tools/workflows/nodes.py` so you don't maintain separate TS and Python agent prompts.

 |

---

### 4. How Skills Should Map to Your 4 Deep Agents

Instead of exposing a generic `td skill` / `skills.ts` lookup tool where agents guess which skill to read, use **Deterministic Context Injection** in LangGraph:

1. **Init (No Skills / No LLM):**
* Pure Python/bash execution (`git fetch`, `vector_store.py`, `pnpm` link). 0 tokens.




2. **Issue Generator Agent:**
* **Hardwired Prompt:** `AGENT_CONTRACT.md` + `issue-authoring.md` + `plan-template.md`.


* **Dynamic Skill Injection:** None needed—just `repo.read_agent_context` and `td.init_and_rag` outputs.




3. **Init Coder Agent:**
* **Hardwired Prompt:** `AGENT_CONTRACT.md` + cleaned `lane-coder/SKILL.md` (focused purely on surgical code edits and test writing, zero git/worktree ceremony).


* **Conditional Skill Injection (Deterministic):** Inspect the Issue's `Target Files Allowlist` before starting the Coder node:
* If allowlist contains `.tsx`, `.css`, or `frontend/` $\rightarrow$ automatically append the condensed `ui-craft` guidelines (`visual_guidelines.json`).


* If allowlist is pure `.py` / backend $\rightarrow$ **do not load any UI/UX skills** (saves ~4k tokens per turn).




4. **Issue + Code Auditor Agent:**
* **Pre-Gate (No Skills):** Runs `audit-ai-slop.py`, `ast_audit.py`, `detect-antipatterns.mjs`, and `scope_gate.py`.


* **Hardwired Prompt (Only after pre-gate passes):** `REVIEW_INSTRUCTIONS.md` + `review-pr.md`.


* **Conditional Skill Injection:** If `check-visual-changes.ts` detects DOM/CSS changes, inject `review-ux.md` + the pixel diff output from `impact-visual-diff.ts`.





