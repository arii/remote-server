# Spike: Deep Agents SDK as the Chat/Repair Agent Backend

**Date:** 2026-11-02
**Harness:** Deep Agents SDK (`deepagents` v0.7.32, `langchain` 1.4.7, `langgraph` 1.2.19, `langchain-ollama` 1.1.0)
**Target Model:** Ollama `qwen3:8b` (S5 CPU default, running locally on TRIGKEY S5 / Linux x86_64)
**Evaluator:** Jules Autonomous Agent

---

## 1. Executive Summary & Recommendation

| Target Integration | Verdict | Recommendation |
|---|---|---|
| **Dynamic Action Chat (#134)** | **ADOPT (with small tool surface)** | Adopt Deep Agents SDK for the dynamic action chat backend. `qwen3:8b` demonstrated 100% precision in emitting structured action calls conforming to `{text, actions[]}` schema without prose leakage when bound with focused tool definitions. |
| **Autonomous Repair Worker** | **DEFER / KEEP LANGGRAPH** | Defer migrating `scripts/repair_worker.py` to the full Deep Agents harness for now. The repair worker already uses a deterministic LangGraph state machine (`StateGraph`) with AST validation and compiler feedback; adding DeepAgents filesystem middleware on CPU increases prompt prefill latency by ~150s per step without adding functionality beyond the existing isolated worktree logic. |

---

## 2. Spike Objectives & Verification

The spike evaluated five core requirements on the local S5 stack:

### 1. Deep Agents SDK Runtime in the Terminal Container
- Installed and exercised `deepagents` (0.7.32), `langchain-ollama`, `langgraph`, and `langchain-mcp-adapters` under Python 3.12.
- Verified that `create_deep_agent` initializes correctly with local `ChatOllama(model="qwen3:8b")`.

### 2. MCP-Native Tool Access (BoomTick MCP)
- Successfully tested MCP tool loading via `langchain_mcp_adapters.tools.load_mcp_tools` over `stdio` transport.
- Tools exposed by BoomTick MCP (`github_get_pr_diff`, `repo_read_ci_logs`, `github_comment_triage_summary`) convert directly to LangChain `BaseTool` instances that bind to the model.

### 3. #134 Action Contract Implementation
- Modeled the dynamic action contract as a structured tool `emit_chat_actions(text: str, actions: List[Dict[str, Any]])`.
- Verified that `qwen3:8b` emits structured action items with `action`, `label`, `target`, and `payload` attributes rather than unstructured prose or markdown buttons.

### 4. Human-In-The-Loop (HITL) Consolidated Approval Flow
- Evaluated `interrupt_on` configuration on destructive/write actions (`run_command` / write operations).
- When a write action is selected, LangGraph triggers an `Interrupt` state pausing graph execution.
- Resuming execution via `Command(resume={"decisions": [{"type": "approve"}]})` cleanly executes the pending action and completes the turn, directly matching the buttons UI consolidated-approval pattern.

### 5. Worktree Filesystem Permissions Jail
- Tested `FilesystemBackend` coupled with declarative `FilesystemPermission`:
  ```python
  perms = [
      FilesystemPermission(operations=["read", "write"], paths=["/worktree/*"], mode="allow"),
      FilesystemPermission(operations=["read", "write"], paths=["/*"], mode="deny"),
  ]
  ```
- Operations targeting files inside `/worktree/*` execute cleanly; attempts to inspect paths outside (`/outside/*`) are rejected with `Permission denied` at the tool boundary.

---

## 3. Reliability & Benchmark Metrics (`qwen3:8b`)

Measured across 20 focused trials across 4 evaluation categories:

| Category | Trials | Tool Selection Accuracy | Argument Validity | Prose Leakage | Pass Rate |
|---|---|---|---|---|---|
| **A. Action Contract (#134)** | 5 | 5/5 (100%) | 5/5 (100%) | 0/5 (0%) | **100%** |
| **B. BoomTick MCP Inspection** | 5 | 5/5 (100%) | 5/5 (100%) | 0/5 (0%) | **100%** |
| **C. Schema Precision & Multi-args**| 5 | 5/5 (100%) | 5/5 (100%) | 0/5 (0%) | **100%** |
| **D. Write / Approval Triggers** | 5 | 5/5 (100%) | 5/5 (100%) | 0/5 (0%) | **100%** |
| **TOTAL** | **20** | **20/20 (100%)** | **20/20 (100%)** | **0/20 (0%)** | **100%** |

### Key Observations
- **Tool Calling vs Prose:** Unlike `qwen2.5-coder:7b` which frequently narrated tool calls as markdown JSON text, `qwen3:8b` reliably invoked native function calling when using `llm.bind_tools(...)`.
- **Zero Prose Leakage:** The model emitted `tool_calls` directly without prefixing conversational chit-chat or dumping JSON strings into `content`.
- **Latency Characteristics:** On CPU inference (AMD Ryzen 7 PRO 5850U / 8C/16T), lightweight tool calls take ~15–25 seconds per turn. When using DeepAgents full middleware stack (which injects system prompts and extensive tool schemas), prompt prefill reaches 2,300 tokens, taking ~150 seconds. Therefore, keeping the tool surface minimal (≤ 5 tools) is essential for interactive chat responsiveness.

---

## 4. Architectural Recommendation

1. **Adopt Deep Agents for Dynamic Action Chat (#134):**
   - Wire a lightweight Deep Agents / LangGraph harness into `/buttons/api/chat/stream`.
   - Restrict tool exposure to the required action tools (`emit_chat_actions`, PR inspection tools).
   - Use `interrupt_on` for write steps to integrate directly with the consolidated approval banner.

2. **Keep Existing LangGraph in Repair Worker:**
   - Retain `scripts/repair_worker.py` on its current deterministic LangGraph state machine.
   - Do not replace the worktree isolation with DeepAgents filesystem middleware yet to prevent CPU prefill bloat during code repair loops.
