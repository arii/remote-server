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
