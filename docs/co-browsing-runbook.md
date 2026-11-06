# Co-browsing runbook: live preflight via `agy` (follow_up Issue 12)

End-to-end proof that the shared browser head, the pause/resume
lock, and `td audit-impact` visual diffs work as one loop. Run from
the **terminal container** (compose network), where `agy` lives::

    cd /srv/remote-server
    python3 scripts/verify-co-browsing-preflight.py

Expected: six `[PASS]` lines and `"ok": true`. Exit 0 = green,
1 = a stage failed, 2 = environment/tooling missing (message says
what).

## Environment knobs (all optional)

- `BROWSERLESS_HTTP` (default `http://browserless:3000`)
- `BROWSERLESS_TOKEN` (appended to the CDP websocket URL)
- `AGENT_API` (default `http://buttons:8000/api/agent`)
- `PREVIEW_URL` (default `<BROWSERLESS_HTTP>/debugger/`)
- `TD_BIN` + `TD_CWD` (default `td`; the installed td predates
  `--preview-url`, so until it is reinstalled point at a checkout:
  `TD_BIN="python3 dev_tools/cli.py"
  TD_CWD=/srv/repos/boomtick/cli`)
- `BROWSERLESS_ENDPOINT` (default derived from the above; after the
  next terminal recreate the compose default `ws://browserless:3000`
  applies and the export is unnecessary)
- `REPO` (default `/srv/remote-server`; needs a `frontend/src/index.css`)

## What each stage proves

1. `pressure` — browserless head up and accepting sessions.
2. `cdp_snapshot` — Playwright `connect_over_cdp` (no local
   Chrome), preview navigation, DOM metrics, screenshot to tmp.
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
- `pressure` / `cdp_snapshot` errors: `docker compose ps
  browserless`; `curl http://browserless:3000/pressure` from the
  terminal container. Never bare `docker compose up -d` (it
  recreates the terminal service and kills live sessions);
  single-service `docker compose up -d browserless` only.

## CI shape

`tests/test_co_browsing_e2e.py` wraps the same stages as unittest
methods: pure helpers run everywhere; live stages skip unless the
stack is reachable (`E2E_COBROWSING=1` guard inside — set it when
running where browserless + backend are up).
