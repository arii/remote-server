# Browser setup (browserless shared head)

Persistent containerized Chromium for visual co-browsing and
automated visual audits (follow_up.md Issue 8).

## Bring-up

```bash
docker compose up -d browserless
docker compose ps browserless   # must show Up (healthy has no check; see pressure)
```

Single-service start only — never bare `docker compose up -d`
(it recreates the terminal service and kills live zellij sessions).

## Validate

```bash
# Pressure API (no auth on the compose network / loopback)
curl -fsS http://127.0.0.1:3000/pressure
# CDP version endpoint
curl -fsS http://127.0.0.1:3000/json/version | head -c 300
# From inside the terminal container (compose-network path agents use)
docker compose exec terminal curl -fsS http://browserless:3000/pressure
# Caddy route (reload first after Caddyfile edits — zero downtime,
# no container recreate):
docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose exec terminal curl -fsS http://caddy:8080/browser/ -o /dev/null -w '%{http_code}\n'
```

Human UI: `https://<TERMINAL_DOMAIN>:<TERMINAL_PORT>/browser/`
after GitHub OAuth renders the Browserless interactive debugger.
Agent CDP: `ws://browserless:3000` (compose network) or
`ws://<tailscale-ip>:3000?token=...` (mesh, when
`BROWSERLESS_TOKEN` is set).

## Notes

- `shm_size: '2gb'` prevents Chromium SIGBUS crashes on heavy DOM snapshots.
- `PREBOOT_CHROME=true` keeps a warm instance for sub-second connects.
- The Chromium process recovers internally; the container needs no restarts.
- `BROWSERLESS_TOKEN` (see `.env.example`) secures the direct CDP
  endpoint. Human `/browser/` access is gated by oauth2-proxy upstream.
