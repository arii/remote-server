# Co-browsing runbook: live preflight via `agy` (follow_up Issue 12)

End-to-end proof that the shared headed-browser head, the
pause/resume lock, and `td audit-impact` visual diffs work as one
loop. Run from the **terminal container** (compose network), where
`agy` lives::

    cd /srv/remote-server
    python3 scripts/verify-co-browsing-preflight.py

Expected: six `[PASS]` lines and `"ok": true`. Exit 0 = green,
1 = a stage failed, 2 = environment/tooling missing (message says
what).

## Environment knobs (all optional)

- `BROWSER_HTTP` (default `http://browser:4444`) — Selenium Grid
  base of the `browser` service. The service must exist; no fail-safe.
- Human stream: the `/cdp-stream` viewer (Caddy -> buttons
  backend), which attaches to the live agent session when one
  exists, else opens its own. First input pauses the agent.
- `AGENT_API` (default `http://buttons:8000/api/agent`)
- `PREVIEW_URL` (default `http://caddy:8080/buttons/` — the console
  itself, the meaningful visual-audit target)
- `TD_BIN` + `TD_CWD` (default `td`; installed td 0.19.2+
  ships `--preview-url`, verified 6/6 PASS 2026-11-06 — only point
  at a checkout if the installed td regresses:
  `TD_BIN="python3 dev_tools/cli.py"
  TD_CWD=/srv/repos/boomtick/cli`)
- `BROWSER_ENDPOINT` (default `http://browser:4444` via compose;
  the viewer bridge opens its own Grid session from this base URL)
- `REPO` (default `/srv/remote-server`; needs a `frontend/src/index.css`)

## What each stage proves

1. `grid_ready` — Selenium Grid up with a ready node and a free
   slot (single session: `SE_NODE_MAX_SESSIONS=1`).
2. `cdp_snapshot` — Grid session + Playwright `connect_over_cdp`
   (no local Chrome), preview navigation, DOM metrics, screenshot
   to tmp; the session is always deleted so the slot never wedges.
3. `screenshot_integrity` — the shot is a non-empty PNG.
4. `pause_halts` — `POST /api/agent/pause` sticks
   `PAUSED_BY_USER` + flags a DOM refresh (the halt the pipeline
   honors; the wait itself is unit-covered in
   `tests/test_co_browsing_pause.py`).
5. `resume_refresh` — `POST /api/agent/resume` returns to
   `RUNNING` with a re-snapshot scheduled.

Agent-initiated handoffs (#1097) ride the same contract: the agent
parks via `POST /api/agent/handoff/request` (reason + stuck URL),
the drawer shows an explicit waiting banner instead of the paused
one, and the human's Hand Back (`POST /api/agent/handoff/resolve`)
clears the wait and resumes with the mandatory DOM re-snapshot —
so the viewer shows the post-handoff state. See the
`cobrowse_handoff_specialist` deep-agent skill for the owner loop.
6. `td_impact` — `td audit-impact --preview-url` on a scratch
   worktree with a one-line CSS probe returns pure-JSON stdout
   with a reachable `visual_diff` (DOM counts, screenshot path).

## Interpreting failures

- Stage `ENV_MISSING` on `td_impact` mentioning stale td: reinstall
  or repin td (see `docs/follow-ups.md` §3), or export `TD_BIN` /
  `TD_CWD` as above.
- `pause_halts` / `resume_refresh` HTTP errors: the backend is down
  or unreachable — `docker compose ps`, then check buttons health.
- `grid_ready` / `cdp_snapshot` errors: `docker compose ps
  browser`; `curl http://browser:4444/status` from the terminal
  container. A `no free grid slot` failure means a leaked session
  holds the single slot — list sessions via the Grid UI and
  `DELETE /session/<id>`. Never bare `docker compose up -d` (it
  kills the terminal and live sessions); single-service
  `docker compose up browser` only.

## Viewer architecture (post-#1008)

The `/cdp-stream` viewer (`backend/api/routers/browser_view.py`) is
the single viewer-side CDP client: it acks every `screencastFrame`
immediately on receipt (the compositor blocks until ack), flushes
only the latest frame while a UI send is in flight, reattaches +
restarts the screencast on `targetCrashed`, and retries input ops
once after reattach. Human input pauses the agent via
`POST /api/agent/pause`. Container side: `browser` runs with
`shm_size: 2gb` + `init: true`, and Chrome sessions pass
`--disable-dev-shm-usage`.

Live self-test against the real Grid (needs no browser):

    docker compose exec -T buttons \
      python3 -m backend.api.routers.browser_view --self-test

Expected: `frames=<n> ... typed='hi42'` + `OK` (proves frames flow,
click + atomic `insertText` typing land, cleanup runs).

## CI shape

`tests/test_co_browsing_e2e.py` wraps the same stages as unittest
methods: pure helpers run everywhere; live stages skip unless the
stack is reachable (`E2E_COBROWSING=1` guard inside — set it when
running where the headed browser + backend are up).
