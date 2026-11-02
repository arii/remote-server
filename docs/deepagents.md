# DeepAgents direction — succeeding Pi's jobs

DeepAgents (LangChain) is the planned successor for the work Pi does
today in this stack: repo briefings, triage support, and the
buttons-page suggestion flows (`Suggest next actions`, `Ask Pi` — see
`docs/buttons.md`). Pi + Ollama stays active until a DeepAgent reaches
parity on those flows; nothing here retires Pi yet.

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

1. **Verify the harness on Nebius.** Use `NEBIUS_API_KEY` (set it in
   `.env`, never committed; passed into the terminal container like
   `GEMINI_API_KEY`) for initial bring-up only. Treat every call as
   metered: minimal prompts, minimal iterations.
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

## Open questions

- Python vs TypeScript scaffold for the first agent (both quickstarts
  installed; undecided).
- Where DeepAgent services run long-term (terminal container vs own
  service) — decide after harness verification.
