# Buttons — operator console

Phone-first SPA + FastAPI console at `/buttons` (same TLS + GitHub OAuth
gate as the terminal). Every destructive action runs a fixed allowlisted
command with a confirm gate; reads never mutate. As-built state: this
file. Symbol anchors (file + route/function name), never line numbers.

## Layout

`frontend/src/App.tsx`: `Tab`, `TABS`, `SECONDARY_TABS`.

- **Now** (`views/NowView.tsx`) — triage inbox. Attention queue
  (`fetchAttention`, `fetchInbox`, `nowPollMs`, `SNOOZE_PRESETS`) on
  top, then Review and Jobs sections inside Now, not as routes
  (`views/ReviewView.tsx`, `views/JobsView.tsx`, `views/InboxStrips.tsx`).
  Rollup repo (`repo=all`) aggregates here; per-repo views gate behind
  `RollupGate`.
- **Chat** (`views/ChatView.tsx`, `pumpStream`) — SSE agent chat
  (`api/chat/stream`, `api/chat/resume`, `api/chat/abort`,
  `api/chat/route-hint`). Backend picker (`CHAT_BACKEND_OPTIONS`:
  auto / local / cloud), prompt starters (`CHAT_PROMPT_CHIPS`),
  approval cards gate writes. Agent runs drawer reads
  `api/agents/runs`.
- **Briefing** (`views/BriefingView.tsx`) — structured repo briefing
  (`fetchBriefing` → `GET /api/briefing?repo=`): attention, open PRs,
  reviews, conflicts, overlaps, failing runs, next actions, recent
  activity (`cleanBriefingLine`). Digest + notification controls
  (`fetchNotificationStatus`, `triggerDigest`,
  `fetchNotificationPreferences`, `fetchDigestSchedule`).
- **History** (`views/HistoryView.tsx`, secondary nav) — audit trail:
  jobs + ledger rows (`fetchJobs`, `fetchLedger`, `toHistoryRows`,
  `historyRowMatches`) plus `views/ObservabilitySection.tsx`.

Header (`App.tsx`, `components/RepoPicker.tsx`): repo picker, dirty-diff
chip (`GET /api/repo/diff`), health pill (`healthSummary`,
`HEALTH_POLL_MS`, `singleFlight`), `SyncBanner` offline outbox,
Terminal/Code links.

## API mount

Browser calls `/buttons/api/*`; `caddy/Caddyfile` (`handle /buttons*`,
`strip_prefix /buttons`) strips the prefix so the backend sees `/api/*`.
Frontend `apiClient.ts` (`apiUrl`, `BASE_URL=/buttons/`) uses relative
`api/…` paths. Backend `backend/server.py` registers
`backend/api/routers/*` via `include_router`; `GET /health` is the
liveness probe.

Routes by router (verbs + paths; see router files for shapes):

- `system.py` — `GET /api/system/health`, `GET /api/system/stats`.
- `repo.py` — `GET /api/repos`, `GET /api/repo/status|diff|files|health`.
- `attention.py`, `inbox.py` — `GET /api/attention`,
  `POST /api/attention/snooze|unsnooze`, `GET /api/inbox?repo=`.
- `pulls.py` — `GET /api/prs|pr-details|pr-diff`, `POST /api/pr-action`
  (merge / update / approve / request-changes, server re-checks
  fresh PR state at tap time; fix-ci retired, #428).
- `issues.py` — `GET /api/issues|labels`, `POST /api/create-issue`,
  `POST /api/issue-draft|issue-label`, repair worker
  (`POST /api/repair/dispatch|abort|schedule`,
  `GET /api/repair/status|verify-command`).
- `jobs.py` (`RunRegistry` in `backend/exec/engine.py`) —
  `GET /api/jobs`, `GET /api/jobs/stream` (SSE),
  `POST /api/jobs/run|run-td|run-plan|clear`,
  `GET /api/jobs/runs/{rid}|runs/compare`,
  `GET /api/jobs/runs/{rid}/stream`, `POST /api/jobs/runs/{rid}/cancel`,
  `GET /api/jobs/plans|plans/{name}`, schedule CRUD
  (`GET|POST /api/jobs/schedule`, `DELETE /api/jobs/schedule/{sid}`).
  Single-flight: a second run 409s while one is active; cancel first.
- `chat.py`, `agent.py`, `agents.py` — `GET|POST /api/chat/stream`,
  `GET /api/chat/route-hint`, `POST /api/chat/abort|resume`,
  `GET /api/pi-progress/{rid}`, `GET /api/agent/providers`,
  `POST /api/agent/turn|abort`, `GET /api/agents/runs…|budget|health`.
- `briefing.py` — `GET /api/briefing?repo=`, `GET /api/status`,
  `GET /api/ledger?kind=&limit=&since=`, `POST /api/ledger/clear`
  (truncates + tombstone; gated like jobs/clear on
  `{intent: "ledger-clear", confirmed: true}`; briefing reads are not
  ledgered).
- `ops.py` — `POST /api/terminal/takeover`,
  `GET /api/workflow-health?repo=`, `POST /api/clear`, `GET /api/health`.
- `notify.py` — `POST /api/webhook` (GitHub push auto-pull, see below),
  `GET|POST /api/notifications/status|test|trigger-digest|preferences|digest-schedule`.
- `deferred.py` — `GET|POST|DELETE /api/deferred`, `POST /api/deferred/bulk`.

## Notifications

Push via ntfy (`NTFY_TOPIC`, `NTFY_URL`, `NTFY_TOKEN`) and/or a generic
JSON webhook (`NOTIFICATION_WEBHOOK_URL`); see `.env.example`. Digest
cadence via `DIGEST_INTERVAL_HOURS` / `POST /api/notifications/trigger-digest`;
event set via `NOTIFY_EVENTS`. Unconfigured state surfaces in Briefing
(`digestUnconfiguredReason`). The check loop (#822) is a `check-notifications`
jobs action on a `quarter-hourly` schedule, one per checkout, seeded
automatically — ci_fail/conflict pushes and the interval digest fire from
the shared 60s tick with state dedup; failed fetches keep prior keys and
failed digest sends never stamp (#532). Delete a schedule to stop its repo
(deletes stick; new checkouts seed on a later tick).

## Webhook auto-pull

`POST /api/webhook` (edge: `/buttons/api/webhook`, the one
`--skip-auth-route` in `docker-compose.yml`) pulls `main` on push:
HMAC-SHA256 (`WEBHOOK_SECRET`, fail closed when unset), push-only,
allowlisted repos, `--ff-only`. Pull only — never rebuild, never
install. Last pull per repo shows in `GET /api/status`.

## Architecture

```text
browser -> oauth2-proxy (TLS + GitHub OAuth)
        -> caddy:8080 (compose network only)
           -> /         -> terminal:7681
           -> /buttons* -> buttons:8000 (prefix stripped)
           -> /code*    -> code-server:8080
```

`buttons` is one image (`Dockerfile`): Vite SPA (`VITE_BASE=/buttons/`,
served as static files) + FastAPI backend (`backend/server.py`) on
`:8000`, compose-network only. Ledger persists on
`buttons-data:/var/lib/buttons` (never in a checkout). The container
mounts the Docker socket (root-equivalent, same trust as terminal) and
the checkouts at host-identical paths; never publish its port or bypass
oauth2-proxy.

## Security properties

- Allowlist, not a shell: fixed argv per action, validated repo/PR/enum
  inputs, shlex-quoted interpolations. A reached page can only run what
  the routers expose.
- Same gate as the terminal: no separate login. If oauth2-proxy is down,
  the console is down too.
- Webhook exception, tightly scoped: exact-path POST skip-auth + HMAC +
  repo/branch allowlist + fast-forward only; it can only pull checkouts.

## Deploy

On the S5, from the stack checkout:

```bash
git pull --ff-only
docker compose up -d --build buttons
```

Verify: `/` still opens the terminal, `/buttons` shows Now, Status pill
reads `N/M containers up`. Rollback: repoint oauth2-proxy upstream to
`terminal:7681`, `up -d oauth2-proxy`, `down caddy buttons`.
