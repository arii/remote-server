# Agent harness (active path)

**Status:** the deep-agent lane is the active harness. (History: a
2026-10-22 evaluation compared Pi/OpenClaw/LangChain/etc. and Pi was
activated 2026-10-27; that evaluation is superseded — the Pi surface
is being removed and all agent work flows through `deep-agent/`.)

**Hardware:** TRIGKEY S5 — Ryzen 7 PRO 5850U (8C/16T Zen 3, no
AVX-512), 32 GB DDR4, no dGPU. CPU-only Ollama inference.
Estimated (measure after the terminal rebuild, #246): 8B Q4 ≈
6–12 tok/s, 14B Q4 ≈ 3–6 tok/s. Production default is no-think
mode — a qwen3 thinking trace costs 15+ min per prompt on this box.

## Architecture

- Investigate: `deep-agent/ask_investigator.py` (Ask) and
  `deep-agent/deep_chat.py` (chat turns) run read-only tools against
  the repo, then answer. Tools: `read_repo_file`, `grep_repo`,
  `list_dir`, `search_docs`, `pr_state` — all deterministic-first
  over `td`/`gh`/subprocess JSON, local Ollama for judgment.
- Propose: when the work needs writes, the agent calls
  `propose_action` once (kind `issue` or one allowlisted `td`
  command) and stops. It cannot run, change, or deploy anything.
- Approve: Ari approves or rejects from the phone. Approval
  dispatches execution; rejection ends the turn.
- Execute (bridge under construction): an approved proposal opens a
  BoomTick `agent_dispatch` worktree session that implements,
  tests, and opens the PR.
- Review: the executor never reviews its own work. Review stays
  GitHub-native (PR comments, request-changes round-trips, bounded
  rounds, then escalate to Ari).

## Routing ladder

Deterministic command → Ollama `qwen3:8b` → Ollama `qwen3:14b` →
API-token LLM. Encoded in `deep-agent/model_router.py`:

- Try a deterministic command first; the LLM handles judgment,
  explanation, and summarization only.
- `ASK_MODEL` (default `qwen3:8b`) is the interactive tier;
  `ASK_MODEL=qwen3:14b` selects the significant-local tier (default
  once tok/s is measured).
- The API-token path (Nebius `zai-org/GLM-5.3-Flash` today;
  provider decision pending) is used only for write-mode
  multi-step execution, context beyond the 8b window, or judged
  output the 8b gate does not cover.
- `NEBIUS_ONLY=1` forces cloud; `OLLAMA_STRICT=1` fails instead of
  falling back (offline operation). Specs never carry secrets —
  the caller passes keys from the environment.

## Observability

Every run is ledgered (`deep-agent/ledger.py`) and traced
(`deep-agent/observe.py` → `traces/*.jsonl`). Read runs with
`deep-agent/runs.py`, render traces with
`deep-agent/trace_view.py` (phone text or self-contained HTML).
See `deep-agent/OBSERVABILITY.md`; the wire contract is
`deep-agent/API_CONTRACT_PROPOSAL.md`, and the SPA wiring recipe
is `docs/agent-integration-guide.md`.

## Eval gate

`deep-agent/eval_ask.py` + `deep-agent/eval_chat.py`: 5 gating
cases (docs-howto, code-locate, pr-state, propose-issue,
propose-td) plus non-gating probes, passing on local Ollama.
The gate covers read-only chat; write-mode cases (proposal
validation, dispatch round-trip, repair convergence) are next
and promote to gating once green on both Ollama and cloud.

## Doc map

- `docs/agent-integration-guide.md` — SPA ↔ agent wiring.
- `docs/boomtick-issues.md` — BoomTick-side tracking.
- `docs/deepagents.md` — Deep Agents guide; `docs/local-ux.md` —
  local preview workflow.
