---
name: td-tools
description: Use BoomTick CLI and RAG first for repo research — query before reading, snapshot via Playwright, keep telemetry deps floating.
---

# TD Tools

Default tool leverage for every research, triage, audit, and implement loop. Query first, read only what the hits cite. Fewer tokens, fresher answers.

## RAG first

- Before bulk `read_file`/`search` for any research, triage, or audit question, run `td rag query --repo-root <repo> --query "<keywords>" --top-k 8` (`/opt/venvs/td/bin/td`). Read only the files the hits cite. `--repo-root` defaults to the git root; pass it explicitly from worktrees or other checkouts.
- Canary: a healthy index returns hits (e.g. 8) for obvious in-repo terms. Zero hits on obvious terms means the index is broken, not that the code is absent — diagnose, don't conclude.
- After any `pip install .../boomtick/cli` upgrade: `td rag index --full` on active checkouts, then re-run the canary. Upgrades can skew transitive deps (seen: OTLP exporter floated while the SDK stayed pinned → `index_available: false`, silent 0-hit index).
- Skew symptoms (`index_available: false`, 0 hits, `ImportError: ReadableLogRecord`): check `pip check` for opentelemetry mismatches and align the set. Never add `==` pins to fix it — pins caused it.

## Provider verification (no silent fallback)

- Native session agents do the work by default. Nebius is for explicitly delegated REST tasks: probe `$NEBIUS_BASE_URL/models` with `$NEBIUS_API_KEY` first; `NEBIUS_MODEL` may be empty in container env, so pick from the listing. Ollama is probed at `localhost:11434/api/tags`; connection refused means the local-LLM loop is blocked — file an issue, don't reroute silently.
- Log every provider task with evidence links (PR comments, run URLs, token usage) to `docs/llm-provider-scorecard.md` in the repo where the work landed.

## Buttons snapshots (Playwright)

- Live app entry: `http://remote-buttons-1:8000/` + route rewrite `**/buttons/**` → replace `/buttons/` with `/` (prod serves under the `/buttons` base path via caddy; direct backend serves the shell at `/` with assets at `/assets/*`).
- Capture 390×844 and 1440×900 full-page screenshots, then READ the pixels before claiming anything. No BrowserMCP needed for static verification.

## Issues

- Dedup first: `gh issue list --search "<keywords>"` (repo `arii/remote-server`). File with a severity, verified `file:line`, expected vs actual, alternatives considered, and prior art checked.
