# Browser Setup (Solution A: headed Chromium)

The shared browser head is a real headed Chromium
(`selenium/standalone-chromium`, pinned digest in
`docker-compose.yml`) running on a virtual display inside the
`browser` service:

- Agent CDP: Selenium Grid on `http://browser:4444` (compose
  network). Agents open a Grid session with the agent-profile caps
  (`--user-data-dir=/data/shared-browser/agent`,
  `--enable-features=WebMCP`) and drive the per-session CDP
  endpoint `/session/<id>/se/cdp`; the session is deleted
  afterwards (`SE_NODE_MAX_SESSIONS=1`, so a leaked session wedges
  the single slot). Idle sessions are reaped after 3600s
  (`SE_NODE_SESSION_TIMEOUT`).
- Human stream: noVNC on `http://browser:7900` (compose network),
  routed by Caddy at `/browser*` (stripped) and embedded in the
  buttons Shared-browser drawer at
  `/browser/vnc.html?autoconnect=1&resize=scale`.
- An always-open personal window (supervisord) keeps a real browser
  on the display even with zero sessions; it uses
  `/data/shared-browser/personal` — never drive it via CDP.
- Profiles live on the host drive at
  `~/.s5-shared-browser/` (bind mount, pre-created as 1200:1201;
  the supervisor conf is likewise host-seeded because the daemon
  resolves bind sources on the host, not in this checkout —
  see the `browser` service comments in `docker-compose.yml`).
- The container hostname is pinned (`shared-browser`) so Chromium's
  Singleton profile lock survives recreates.
- `BROWSER_ENDPOINT` (default `http://browser:4444`) is set on the
  terminal service; `browser_cdp` consumes it as the Grid base
  URL. The service must exist; there is no fail-safe.

Containerized `browserless/chromium` stays decommissioned. For the
extension-pairing alternative see
[`docs/browser-mcp-pairing.md`](browser-mcp-pairing.md).
