# Container durability — what survives a restart, and what doesn't

## What already persists (named volumes, no action needed)

| State | Where it lives |
|---|---|
| Pi sessions + auth | `pi-data` |
| agy binary, state, auth, chats | `gemini-data` |
| Muse CLI auth/settings | `muse-data` |
| gh CLI login (`hosts.yml`) | `gh-data` |
| VS Code settings, extensions, workspaces | `code-server-data`, `code-server-config` |
| Ollama models | `ollama-data` |
| Tailscale identity | `tailscale-state` |
| Buttons audit ledger | `buttons-data` |
| BrowserMCP daemon identity (pairing) | `local-data` via `BROWSER_MCP_HOME` |

## What the durability work added

- **Shell history**: `HISTFILE` points at `/root/.local/share/bash_history`
  (`local-data`). Note: concurrent shells overwrite on exit (last-writer-wins).
- **Zellij sessions**: `XDG_CACHE_HOME=/root/.local/cache` (`local-data`) plus
  `session_serialization` + viewport serialization in
  `terminal/config.kdl`. Layout, cwds, rerun commands, and scrollback come
  back on `zellij attach --create main`. **Live processes do not** — panes
  re-run their commands fresh (a `pi` pane restarts `pi`; its history on disk
  is intact but the conversation is not resumed).
- **tmux**: no persistence mechanism exists (no resurrection feature). Use
  zellij for sessions that must survive; tmux stays for ad-hoc use.
- **vim**: `terminal/vimrc.local` sets `viminfofile` under `local-data`.
- **Logs**: every service caps json-file logs at 3×10MB (`x-logging` anchor).
- **Health**: terminal (existing), code-server, buttons, caddy (admin API),
  ollama (`ollama list`). Deliberately no *in-container* healthcheck for
  oauth2-proxy — its image ships no shell/curl, so `CMD-SHELL` is
  genuinely awkward — but the edge IS probed: `GET /ping` answers `200 OK`
  unauthenticated, and a host cron job (`scripts/edge-liveness-watch.sh`,
  installed by `scripts/install-edge-liveness-cron.sh`, every minute)
  restarts the proxy after 3 consecutive probe failures and ledgers the
  event to cron output + syslog (`journalctl -t edge-liveness`). That
  covers the live-but-dark wedge (bad renewal, upstream hang) that
  `restart:` never fires on. Deliberately none for tailscale (nothing
  gates on it) and browser-relay (its upstream daemon is agent-managed,
  not compose-managed).
- **Startup order**: `depends_on` with `service_healthy` gates the chain
  (edge waits for backends). Generous `start_period`s cover slow boots
  (Ollama model load, first-run extension installs).
- **Ollama memory**: capped at 16g (box has 26GB; a 14b model needs ~10GB).
  A bigger model gets OOM-killed here instead of starving the box.

## What NEVER persists (by design)

Running processes. No container mechanism can revive a live agent, a tmux
server, or an in-flight command across a stop — only their data (history,
session files, workspaces, configs) carries over.

## Go-time procedure (after staging, on your word)

Run from the S5 host checkout (`/home/ari/remote-server`) — never from the
phone terminal, whose `/srv/remote-server` view resolves `./` binds to paths
the daemon cannot see:

```sh
docker compose build terminal   # picks up Dockerfile changes (vimrc, mkdirs, latest boomtick-mcp)
docker compose up -d            # recreates only stale services, in dependency order
```

Expected disruption: each service drops for seconds during its recreate
(edge and terminal briefly unreachable; zellij/tmux panes restart;
BrowserMCP pairing survives via the persisted daemon identity).
Verify after: shell history present, `zellij attach` revives layout,
`vim` jumps/registers intact, `/code` extensions and pairing work.

## Cutoff fire-drill (issue #800)

Proves the console survives the actual cutoff event (muse-data
volume gone, Muse CLI failing) while we can still fix what it
finds. Run from the repo checkout:

```sh
./scripts/cutoff-drill.sh          # static + presence probes, no LLM spend
./scripts/cutoff-drill.sh --live   # plus one real agy turn + narration (metered/slow)
python3 -m unittest tests.test_cutoff_drill  # offline static suite (same assertions)
```

The drill masks `muse` with a failing tripwire shim, then reports
per-surface ok/FAIL/SKIP for: chat turn via agy, ask pipeline,
briefing narrator, eval gate. SKIP means the live prerequisite was
absent (stack down, Ollama unreachable) — rerun with the stack up
before calling it green. Any tripwire hit (a surface shelling out
to `muse`) fails the drill outright.

On a FAIL line: file one follow-up issue per red surface with the
failing check and the hidden dependency it names; do not fix it in
the drill PR. On green: no action — the cutoff flip stays
`AGENT_DEFAULT_PROVIDER=agy` on the buttons service (issue #399),
and agy continuity evidence lives in `docs/agy-continuity.md`.

Keyless proof (issue #889): `cutoff-proof.yml` runs
`scripts/cutoff-drill.sh --proof` in CI — same four surfaces with
cloud credentials scrubbed and asserted absent, `OLLAMA_STRICT=1`,
and skips treated as failures — against a local Ollama service
with the default model. It runs on PRs touching provider/model/turn
code plus nightly. Run `./scripts/cutoff-drill.sh --proof` locally
with Ollama up for the same proof (the live agy turn stays a local
`--live` gate: it needs stack OAuth, which CI never holds).
