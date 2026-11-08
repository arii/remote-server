---
name: td-tools
description: Use BoomTick CLI and RAG first for repo research — query before reading, snapshot via Playwright, keep telemetry deps floating.
---

# TD Tools

Default tool leverage for every research, triage, audit, and implement loop. Query first, read only what the hits cite. Fewer tokens, fresher answers.

## RAG first

- Before bulk `read_file`/`search` for any research, triage, or audit question, run `td rag query --query "<keywords>"` (repo root, `/opt/venvs/td/bin/td`). Read only the files the hits cite.
- Canary: a healthy index returns hits (e.g. 8) for obvious in-repo terms. Zero hits on obvious terms means the index is broken, not that the code is absent — diagnose, don't conclude.
- After any `pip install .../boomtick/cli` upgrade: `td rag index --full` on active checkouts, then re-run the canary. Upgrades can skew transitive deps (seen: OTLP exporter floated while the SDK stayed pinned → `index_available: false`, silent 0-hit index).
- Skew symptoms (`index_available: false`, 0 hits, `ImportError: ReadableLogRecord`): check `pip check` for opentelemetry mismatches and align the set. Never add `==` pins to fix it — pins caused it.

## Buttons snapshots (Playwright)

- Live app entry: `http://remote-buttons-1:8000/` + route rewrite `**/buttons/**` → replace `/buttons/` with `/` (prod serves under the `/buttons` base path via caddy; direct backend serves the shell at `/` with assets at `/assets/*`).
- Capture 390×844 and 1440×900 full-page screenshots, then READ the pixels before claiming anything. No BrowserMCP needed for static verification.

## Issues

- Dedup first: `gh issue list --search "<keywords>"` (repo `arii/remote-server`). File with a severity, verified `file:line`, expected vs actual, alternatives considered, and prior art checked.
