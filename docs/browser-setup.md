# Browser Setup (CDP-direct shared autonomy)

The shared browser head is a real headed Chromium
(`selenium/standalone-chromium`, pinned digest in
`docker-compose.yml`) on a virtual display inside the `browser`
service. No VNC anywhere in the loop.

- Agent CDP: Selenium Grid at `http://browser:4444` (compose
  network). Agents open a Grid session with the agent-profile caps
  (`--user-data-dir=/data/shared-browser/agent`,
  `--enable-features=WebMCP`, `--disable-dev-shm-usage`,
  `--disable-blink-features=AutomationControlled`,
  `excludeSwitches: [enable-automation]` — measured 2026-11-09:
  `navigator.webdriver` reads false) and drive the per-session CDP
  endpoint `/session/<id>/se/cdp`; the session is deleted
  afterwards (`SE_NODE_MAX_SESSIONS=1`, so a leaked session wedges
  the single slot). Idle sessions are reaped after 600s
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

## Google sign-in (one-time pre-auth)

Google's risk checks fingerprint active CDP sessions, so a cold
profile can stall at login. The agent profile persists on the host
(`~/.s5-shared-browser/agent`), so sign in once and every later
session reuses the cookies:

1. First try the normal path: open `/cdp-stream/`, navigate to
   `https://accounts.google.com/`, and sign in (verified
   2026-11-09: the email step proceeds to the password prompt with
   no automation block on the pinned Grid flags).
2. If a step is ever challenged, fall back to a real browser on
   the same profile: `docker compose stop browser`, copy
   `~/.s5-shared-browser/agent` to a desktop Chrome as its
   `--user-data-dir`, sign in there, copy it back (owner
   `1200:1201`), and `docker compose up -d browser`.
3. Never `--enable-automation` on the Grid caps: it forces the
   automation banner and hardcodes `navigator.webdriver=true`.

## Neko (human-driven alternative)

[Neko](https://neko.m1k1o.net/) (pinned digest in
`docker-compose.yml`) is the WebRTC virtual browser at `/neko/`,
chosen for interactive use: real browser input passes through, so
there is no virtual-cursor or on-screen-keyboard layer to fight,
and mobile browsers get a native-feeling stream. Bring-up:

- `docker compose up -d neko` (pulls ~2 GB on first run).
- Signaling rides the Caddy `/neko*` route (behind the gate);
  WebRTC media uses host UDP 52000–52100, so that range must also
  be forwarded on the router. Caddy serves it `no-store` so client
  updates apply on image bumps.
- `NEKO_NAT1TO1` in compose is the current public IP — update it
  if the residential IP changes.
- Room passwords live in `.env` (`NEKO_PASSWORD`,
  `NEKO_PASSWORD_ADMIN`); dev defaults are in `.env.example`.
- Agentic control stays on the CDP-stream/Grid path: Neko's
  pixels-over-WebRTC gives an agent no DOM, no elements, and no
  scriptable input — the wrong tool for automation. Use Neko for
  hands, CDP-stream for agents.

## Spec deviation (recorded, not rebuilt — issue #1104)

`updated_guide.md` Issue 8 specifies `browserless/chromium` with a
`/browser*` route. The tree instead runs Solution-A (this doc):
headed Chromium via `selenium/standalone-chromium` (pinned digest),
Grid sessions at `http://browser:4444`, and the CDP-direct viewer at
`/cdp-stream`. This is deliberate — the preflight passes 6/6 on
Solution-A — so follow-ups wire the gaps around it (#1104 pause
state, frame headers, flags) rather than migrating to browserless.
