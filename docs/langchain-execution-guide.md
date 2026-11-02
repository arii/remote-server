# LangChain Execution Guide — replacing Pi sessions on the buttons site

How to take one Pi-powered buttons feature and reimplement it as a
deterministic-first LangChain slice. Read `docs/deepagents.md` (the plan:
what, why, status) alongside this file (how, gates, verification).

## 0. Prerequisites

- Read skills in order: `ecosystem-primer` first, then the layer skill it
  points to. Do not write agent code from training knowledge — the APIs move.
- Python env is `deep-agent/.venv` (built from `deep-agent/requirements.txt`).
  Anything importing `langchain*` **must** run under that interpreter, and
  its tests must too (`deep-agent/.venv/bin/python -m pytest ...`); the
  system python3 does not have LangChain and collection will fail.
- Layer choice per the primer: no-LLM logic = plain LangChain tools/chains;
  single-purpose agents = `create_agent`; conditional orchestration =
  LangGraph; long-running planning/memory/subagents = Deep Agents. Add RAG
  (`langchain-rag`) or persistence (`langgraph-persistence`) only when the
  slice needs it.

## 1. Design deterministic-first

Exhaust `td` (`td --help`, `--json` flags) and `boomtick-mcp` schemas before
adding any LLM call — most buttons features reduce to: deterministic gather
(gh/td/subprocess JSON) → optional LLM judgment/explanation →
allowlist-validated output. The LLM summarizes or judges; it never fetches,
never executes. Keep the exact output contracts the server already parses
(`SUGGEST: <td command> :: <reason>`, `PROPOSE issue|td ... END PROPOSE`,
`FINAL:` line) — the parsers (`_parse_suggestions`, `_parse_proposal`) are
already hardened for small-model quirks; reuse them verbatim.

## 2. Implement the slice

- One script per slice in `deep-agent/`, mirroring `ask_investigator.py`:
  strict argv validation, repo allowlist + checkout sandboxing (reject path
  escapes), timeouts on every subprocess, `ledger.record(...)` every run.
- Model routing: use `deep-agent/model_router.py` (`resolve_llm()` +
  `chat_kwargs()`, stdlib-only so system-python tests can import it).
  Explicit prefer wins; otherwise local Ollama (`ASK_MODEL`, default
  `qwen3:8b`) when reachable; otherwise Nebius (`NEBIUS_MODEL`, default
  `zai-org/GLM-5.3-Flash`) when `NEBIUS_API_KEY` exists; else a clear error.
  `NEBIUS_ONLY=1` forces cloud, `OLLAMA_STRICT=1` fails instead of falling
  back. Embeddings stay local (`nomic-embed-text`). Never log specs or
  kwargs — the API key must not reach the ledger or progress files.
- Shared helpers: import from `deep-agent/s5tools.py` — `run`/`run_json`
  (timeout, no shell, capped errors), `gh_json`, `td`, `checkout_for`,
  `read_repo_file`, `grep_repo`. Never re-implement gh/td/file access;
  new code reads validators from `s5tools`, not from slice scripts.
- No reimplementation: if a slice needs a capability that belongs in
  boomtick-mcp/td (new gh/td command, new MCP tool), file an issue in the
  BoomTick repo and wait — do not rebuild it inside `deep-agent/`. Check
  `/srv/repos/boomtick/mcp/.mcp/schemas` and `td --help` first; the
  ALLOWED_TD_COMMANDS list in `buttons/server.py` is the contract of what
  already exists.
- Small-model rules that already proved out: `temperature=0`, think disabled
  (`model_kwargs={"think": False}` — a thinking trace takes 15+ min on this
  CPU box), `recursion_limit` capped (~12), tool args validated server-side
  too (string-typed numbers, digit checks), tool descriptions that teach
  retry-with-shorter-fragment behavior.

## 3. Evaluate before wiring

Follow the `eval_ask.py` pattern: a small fixed question set, run against
Ollama, score pass/fail on output contract (parsed suggestion/proposal
valid), record to the ledger. Do not wire a slice into buttons until it
passes eval on the local model — cloud fallback is for quality gaps found
here, not a substitute for measuring.

## 4. Wire into buttons/server.py

Mirror the `pi_runs` pattern: thread + progress file under `/tmp`
(`pi-progress-<rid>.log`), a progress/result endpoint the page polls, and
output parsing at completion (never trust raw model text past the
validators). Reuse `_validate_td_command` for anything runnable — no new
execution paths. Keep the same confirm-gate UX: propose first, run only on
tap. Coordinate lanes before editing Ask/Suggest/chat-stream code
(see peer sessions; those paths have an active cutover).

## 5. Verify

1. New/updated tests pass under the right interpreter (venv python for
   anything importing `langchain*`, system python3 for the rest).
2. `docker compose --env-file .env.example config --quiet` is clean (if you
   touched compose).
3. `pnpm run lint` green.
4. Live: run the slice once from the terminal container, confirm the ledger
   entry, then hit the buttons endpoint and confirm the page path works:
   `curl http://buttons:8000/buttons/api/<endpoint>` from the terminal
   (read-only endpoints only — never trigger deploys, merges, or recreates
   from verification).
5. Never restart or recreate containers for verification; never `docker
   compose up` from a container-view shell (bind mounts resolve wrong — run
   compose through the buttons container with `-f /home/ari/remote-server/...`
   plus `--no-deps`, or from a host shell).

## 6. Conventions (repo-wide, non-negotiable)

- Single-user box, GitHub OAuth edge; secrets stay in `.env`, never in code.
- Commit authorship and PR flow per `docs/agent-guide.md`; no force-push to
  shared branches; no history rewrites.
- `gh-data` volume persists `gh` auth; `BROWSER_MCP_HOME` persists the
  BrowserMCP daemon identity — don't move either.
