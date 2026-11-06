# Browser Setup (Solution A: headed Chromium)

The shared browser head is a real headed Chromium
(`selenium/standalone-chromium`, pinned digest in
`docker-compose.yml`) running on a virtual display inside the
`browser` service:

- Agent CDP: Selenium Grid on `http://browser:4444` (compose
  network). Agents open a Grid session and drive the per-session
  CDP endpoint `/session/<id>/se/cdp`; the session is deleted
  afterwards (`SE_NODE_MAX_SESSIONS=1`, so a leaked session wedges
  the single slot).
- Human stream: noVNC on `http://browser:7900` (compose network),
  routed by Caddy at `/browser*` (stripped) and embedded in the
  buttons Shared-browser drawer at
  `/browser/vnc.html?autoconnect=1&resize=scale`.
- `BROWSER_ENDPOINT` (default `http://browser:4444`) is set on the
  terminal service; `browser_cdp` consumes it as the Grid base
  URL. The service must exist; there is no fail-safe.

Containerized `browserless/chromium` stays decommissioned. For the
extension-pairing alternative see
[`docs/browser-mcp-pairing.md`](browser-mcp-pairing.md).
