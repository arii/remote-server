---
name: shared_browser_access
description: How to drive the shared headed browser: Grid session lifecycle, CDP endpoint, single-slot discipline, persistent profiles, and the human stream URL.
trigger_nodes:
  - visual_interaction_node
  - td_impact_and_visual_node
---
# Skill: Shared Headed Browser Access

## 1. Where the head lives
- Agent CDP: Selenium Grid at `http://browser:4444` (compose network).
  `BROWSER_ENDPOINT` carries this base URL (set on the terminal service);
  the default is the same address. The service must exist; no fail-safe.
- Human stream: the same head is visible at
  `/browser/vnc.html?autoconnect=1&resize=scale` (noVNC via Caddy).
  An always-open personal window lives on the display, so the stream
  never shows a bare desktop even with zero agent sessions.

## 2. Session lifecycle (mandatory shape)
1. `POST /session` with `{"capabilities": {"alwaysMatch": {
   "browserName": "chrome",
   "goog:chromeOptions": {"args":
   ["--user-data-dir=/data/shared-browser/agent"]}}}}`.
2. Drive the per-session CDP endpoint
   `ws://browser:4444/session/<id>/se/cdp` (Playwright
   `connect_over_cdp`, or raw CDP).
3. `DELETE /session/<id>` when done — no exceptions. The grid runs
   one session (`SE_NODE_MAX_SESSIONS=1`): a leaked session wedges
   the slot for every later run.

## 3. Preferred path: let the driver do it
- `td audit-impact --preview-url <url> --json` opens its own Grid
  session, snapshots DOM + screenshot, and always deletes the
  session. `browser_cdp` is the underlying driver.
- Agent sessions carry `--enable-features=WebMCP`, so consume
  `document.modelContext` tools where a site offers them; CDP
  remains the primary path (works on every site).
- `scripts/verify-co-browsing-preflight.py` proves the whole loop
  (grid ready, snapshot, pause/resume, visual diff); run it from the
  terminal container.

## 4. Profiles and the personal window
- Agent sessions share `/data/shared-browser/agent` (host-backed, survives
  restarts). Do not delete it; do not point sessions elsewhere.
- The always-open personal window uses `/data/shared-browser/personal`.
  Never close it, never drive it via CDP; it belongs to the human.
- Idle sessions are reaped after 3600s. If the slot reports no free
  session and no run is active, `DELETE` the stale session id.

## 5. Human coordination
- The human can take over at any moment: honor `PAUSED_BY_USER`
  (see `co_browsing_state_takeover`), re-snapshot after resume, never
  reuse pre-takeover element references.
- When reporting what the browser shows, cite the noVNC URL from §1
  so the human opens the exact live head.
