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
- `BROWSER_NOVNC` (default `http://browser:7900`) — noVNC stream
  the human watches (same head, routed at `/browser*` by Caddy).
- `AGENT_API` (default `http://buttons:8000/api/agent`)
- `PREVIEW_URL` (default `<BROWSER_NOVNC>/vnc.html?autoconnect=1&resize=scale`)
- `TD_BIN` + `TD_CWD` (default `td`; the installed td predates
  `--preview-url`, so until it is reinstalled point at a checkout:
  `TD_BIN="python3 dev_tools/cli.py"
  TD_CWD=/srv/repos/boomtick/cli`)
- `BROWSER_ENDPOINT` (default `http://browser:4444` via compose;
  `browser_cdp` opens its own Grid session from this base URL)
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

## CI shape

`tests/test_co_browsing_e2e.py` wraps the same stages as unittest
methods: pure helpers run everywhere; live stages skip unless the
stack is reachable (`E2E_COBROWSING=1` guard inside — set it when
running where the headed browser + backend are up).
