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
- Human stream: the CDP-direct viewer served by the buttons backend
  at `/cdp-stream` (drawer iframe, Caddy-routed). It attaches to
  the live agent session when one exists, else opens its own
  (deleted on disconnect). No VNC, no personal window.

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

## 4. Profiles and shared sessions
- Agent sessions share `/data/shared-browser/agent` (host-backed, survives
  restarts). Do not delete it; do not point sessions elsewhere.
- The viewer attaches to YOUR live session when one exists: the human
  sees exactly what you see and their input lands in your page.
  First human input pauses you via the agent_control lock.
- Idle sessions are reaped after 600s (`SE_NODE_SESSION_TIMEOUT=600`).
  If the slot reports no free session and no run is active, `DELETE`
  the stale session id.

## 5. Human coordination
- The human can take over at any moment: honor `PAUSED_BY_USER`
  (see `co_browsing_state_takeover`), re-snapshot after resume, never
  reuse pre-takeover element references.
- When reporting what the browser shows, cite the `/cdp-stream`
  live-view URL from §1 so the human opens the exact live head.

## 6. Drawer agents drive via the buttons API, not raw Grid

The cobrowse drawer runs a repo-free agent session (muse/agy),
and that agent drives the shared page through the buttons
`/api/browser` endpoints: `snapshot` (see it), `activity` (see
what the human just did — observe and learn), `drive`
(navigate/click/type/key/wheel), `state` (who holds the wheel).
Attach-only: it joins the live agent session first, else the
viewer-shared session, else reports browser-busy and asks the
human to open the drawer. Mutating ops refuse while the human
drives (`PAUSED_BY_USER` parks mid-turn too); login walls,
payments, and sends go back to the human. Durable lessons go in
`/var/lib/buttons/browser_notes.md` (shared volume, both
containers; human-editable) — read first, append confirmed
lessons, quote recent `/activity` in the turn summary so the next
turn learns. Repo-free turns carry this brief in-prompt; raw Grid
sessions (§2) stay for terminal-container agents that own their
own head. BrowserMCP is the audit/test harness, never a drive
path.
