# Browser Setup (CDP-direct shared autonomy)

The shared browser head is a real headed Chromium
(`selenium/standalone-chromium`, pinned digest in
`docker-compose.yml`) on a virtual display inside the `browser`
service. No VNC anywhere in the loop.

- Agent CDP: Selenium Grid at `http://browser:4444` (compose
  network). Agents open a Grid session with the agent-profile caps
  (`--user-data-dir=/data/shared-browser/agent`,
  `--enable-features=WebMCP`) and drive the per-session CDP
  endpoint `/session/<id>/se/cdp`; the session is deleted
  afterwards (`SE_NODE_MAX_SESSIONS=1`, so a leaked session wedges
  the single slot). Idle sessions are reaped after 3600s
  (`SE_NODE_SESSION_TIMEOUT`).
- Human stream: the CDP-direct viewer served by the buttons backend
  at `/cdp-stream` (routed by Caddy, embedded in the Shared-browser
  drawer). It attaches to the live agent session when one exists,
  else opens its own (deleted on disconnect). Pointer and keyboard
  input relay as CDP Input events (device-pixel aware, focus
  guaranteed); the first input of a connection pauses the agent via
  the agent_control API, so no takeover overlay is needed.
- The agent profile lives on the host drive at
  `~/.s5-shared-browser/agent` (bind mount, 1200:1201 seluser) and
  persists across session and container restarts.
- The container hostname is pinned (`shared-browser`) so Chromium's
  Singleton profile lock survives recreates.
- `BROWSER_ENDPOINT` (default `http://browser:4444`) is set on the
  terminal service; `browser_cdp` consumes it as the Grid base
  URL. The service must exist; there is no fail-safe.

Containerized `browserless/chromium` stays decommissioned. For the
extension-pairing alternative see
[`docs/browser-mcp-pairing.md`](browser-mcp-pairing.md).
