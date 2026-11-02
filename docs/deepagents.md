# DeepAgents direction — succeeding Pi's jobs

DeepAgents (LangChain) is the planned successor for the work Pi does
today in this stack: repo briefings, triage support, and the
buttons-page suggestion flows (`Suggest next actions`, `Ask Pi` — see
`docs/buttons.md`). Pi + Ollama stays active until a DeepAgent reaches
parity on those flows; nothing here retires Pi yet. End state:
headless task flows move to the harness; Pi keeps the interactive
terminal product (TUI, streaming chat, session browser). Execution
pattern: per-repo subagent fan-out under a thin orchestrator, one
repo per context (proven `deep-agent/triage_fanout.py`).

## What's installed

- `.agents/skills/` — 22 LangChain skills provisioned from
  `langchain-ai/langchain-skills` (integrity hashes in
  `skills-lock.json`). Covers Deep Agents, LangChain, LangGraph,
  middleware/memory/persistence, RAG, eval engineering, and quickstarts.
- Both are **untracked by design** (see `.gitignore`): they live on the
  host checkout, so container recreates are safe, but a fresh clone
  needs them reinstalled from the source repo. Never commit them.
- Start with the `ecosystem-primer` skill (framework selection), then a
  language quickstart (`langchain-python-quickstart`,
  `deepagents-python-quickstart`, or the TypeScript equivalents).

## Model route: Nebius first (sparingly), then local Ollama

1. **Verify the harness on Nebius.** ✅ Done 2026-11-02
   (one stub-tool call on
   `zai-org/GLM-5.3-Flash`, exact tool result returned). Keep using
   `NEBIUS_API_KEY` (in `.env`, never committed) for bring-up only —
   every call is metered: minimal prompts, minimal iterations.
2. **Move to local Ollama** (`qwen2.5-coder`, see `docs/pi-setup.md`)
   once the harness works. Local inference is the default route; Nebius
   is scaffolding, not the runtime.

## Conventions (unchanged)

- The BoomTick MCP `repo` override convention still applies: derive
  `owner/repo` from cwd, pass `"repo"` explicitly off the default, never
  `null` (see `AGENTS.md`).
- Agent commits attribute to `onasafari` per `docs/agent-guide.md`.
- Secrets live only in `.env` (git-ignored). No key material in skills,
  prompts, or docs.

## Pi capability mapping

Goal: cover Pi's jobs with narrow task agents, proving each slice on
the small local model where possible. Pi's evidence: `pi -p --tools
read,grep,find,ls` call sites in `buttons/server.py`, the Suggest/Ask
flows in `docs/buttons.md`, models in `terminal/pi-config/models.json`.

Status: ✅ proven live · ⚙️ partial · 🔬 mapped, unrun · ⏸ stays on Pi.

| Pi capability | Harness equivalent | Status + notes |
|---|---|---|
| Interactive TUI (`pi`, read/write/edit/bash) | None — harness is a headless API | ⏸ stays on Pi; not a migration target |
| Read-only review (diff-scoped, read-only tools) | Agent with built-in `ls`/`read_file`/`grep`/`glob` only; diff passed in, no shell | 🔬 next 8b slice candidate; quality is model-sensitive |
| Suggest next actions (SUGGEST lines → allowlisted td, one-tap approve) | Reuse `td agent suggest -r <repo>` as the deterministic core; LLM adds judgment/explanation only; `interrupt_on` per action mirrors Approve | ⚙️ `td` core verified; LLM runnable-commands unproven on small models — no buttons cutover until proven |
| Ask Pi (read-only investigate → PROPOSE → confirm → validated endpoints) | Read-only investigator subagent → structured proposal → HITL approve → validated run tool | 🔬 open-ended small-model Q&A unproven; server-side validation stays regardless |
| Agent briefing (`pi -p` slow path) | Deterministic briefing (already `td`+Python) + optional summarizer subagent | 🔬 summarizer unrun; briefing data stays deterministic |
| BoomTick MCP tools (PR/CI/Jules ops) | Python tools wrapping the same `td`/`gh` binaries | ✅ proven pattern (harness check, resolver, explainer all use it) |
| Sessions + `/model` switching | `checkpointer` + `thread_id`; model chosen per agent instance | ✅ mechanics proven (HITL spike, resolver); cross-thread Store memory proven |
| Steering + skills (APPEND_SYSTEM.md, skills/) | `system_prompt` + `skills=[...]` with FilesystemBackend | 🔬 skills-on-agent unrun; custom subagents need explicit `skills` |
| Jobs approval gates (Approve/Decline taps) | `interrupt_on` + `Command(resume=...)` approve/reject/edit | ✅ mechanics proven on Nebius; buttons-tap bridge still open |
| Merge-conflict repair (runbook manual path) | Repair worktree + resolver agent + verify + replacement PR | ✅ proven E2E on #148 → #149; now the runbook's canonical path |

## Open questions

- Per-slice small-model quality (measure on Ollama before committing
  each slice). Measured 2026-11-02: `qwen3:8b` makes valid tool calls
  via LangChain; `qwen2.5-coder:7b` narrates calls as JSON text
  instead — use 8b+ for agent loops. Minimal triage explainer
  (`deep-agent/triage_explainer.py`: 2 tools, k=2 RAG) answers
  correctly, fully local.
- Bridge design from HITL interrupts to the buttons Approve taps.
- Where DeepAgent services run long-term (terminal container vs own
  service) — decide after the first slice lands.
