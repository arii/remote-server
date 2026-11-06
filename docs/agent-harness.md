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

## RAG index

Two paths exist — know which one you are debugging:

- **Forward path (agents use this):** `td rag query` reads the repo
  vector index at `<repo>/.td_rag/chroma_db`; `deep-agent/retrieval.py`
  (`query_td_repo` / `search_td`) shells to it, and the `search_docs`
  agent tool goes through the same path. Build/update by commit SHA
  via `td rag index` (markdown-aware chunking); `td init` refreshes
  it as part of worktree provisioning. Coverage: repo docs plus
  Python/TS/TSX sources (`backend/`, tracked `deep-agent/` files,
  `frontend/src/`; tests, fixtures, venvs, `node_modules/`, `dist/`,
  `.td_rag/`, and `.worktrees/` are skipped). The index is a local
  runtime artifact: gitignored, never baked into images.
- **Legacy path:** `retrieval.get_retriever` over
  `deep-agent/chroma_db` (built at buttons startup, `#562`/`#699`).
  Superseded by the `td rag` path; `index_status()` may report
  `failed` while agents still retrieve fine via `td rag`.

Rebuild after docs or code change:

```
td rag index --repo-root /srv/remote-server --json
```

Requirements (both verified 2026-11-06): the `td` doing the
index/query must ship the `rag` command (installed td 0.19.2+ does;
the buttons image lagged on 0.19.1 without it — remote-server#1011),
and `chromadb` + `sentence_transformers` must be importable where the
command runs (the terminal venv lacks them: `td rag index` then
reports chunk counts but persists nothing and queries come back
empty — boomtick#1139 tracks failing loudly instead).

## Model routing (replication notes)

Auto stays local (Ollama `qwen3:8b`, no-think production default —
`CHAT_THINK=1` re-enables thinking); explicit per-turn **Cloud**
forces Nebius. Verified 2026-11-06: direct Ollama generate ~3 s,
Ollama models present, Nebius `resolve_llm(prefer='nebius')` returns
a spec after the Nemotron rate-card fix (#1009). One fail-closed rule
to know: a single unrated metered ledger row blocks ALL Nebius calls
— rate new models in `deep-agent/pricing.py` from the live Token
Factory catalog (`GET /v1/models?verbose=true`). Chat
`prefer=cloud` is the fast path for large tasks; local agentic loops
are CPU-bound (multi-round, large context) with no auto-escalation.

Buttons containers build the index at startup (#562, #699): the
image carries no `chroma_db/` (dockerignored), and
`scripts/buttons-entrypoint.sh` builds it from the baked code in the
background when missing or empty, while the server already serves
(plain restarts skip via the present-index fast path). Queries
served before the build finishes get a "warming, retry shortly"
degraded message instead of empty-silent answers; /health reports
the index state (`ready`/`warming`/`failed`/`missing`). If Ollama
stays unreachable or the build fails, the failure is recorded in
`.index_failed` (transcript in `index_build.log`) and queries keep
degrading until the index is rebuilt. `SKIP_INDEX_BUILD=1` skips
the check for one-off `docker compose run` shells.

## Doc map

- `docs/agent-integration-guide.md` — SPA ↔ agent wiring.
- `docs/boomtick-issues.md` — BoomTick-side tracking.
- `docs/deepagents.md` — Deep Agents guide; `docs/local-ux.md` —
  local preview workflow.
