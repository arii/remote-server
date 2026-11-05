# Cross-repo lane runbook (issue #853)

One-tap lane dispatch for any discovered console repo. The lane
queue is keyed by `(repo, issue)`; each item flows
queue → dispatch → branch/PR/CI → green → merge from the console's
Now → Queue section. remote-server is repo #1 of N — it dispatches
through the same worker and gates as every other repo.

## Onboarding repo N+1

Today: clone + go. Keep it that way.

1. Clone the checkout where the console discovers repos:
   `BUTTONS_REPOS_DIR` (default `/home/ari/repos`), or `/srv/repos`
   in the terminal container. The repo picker and the queue read
   the same discovery (`backend.core.list_repos`).
2. Confirm the checkout shows up: `GET /api/repos` lists it with
   branch + clean flag.
3. Queue an item: Now → Queue → enter the issue number → Queue.
   The queue accepts `123`, `#123`, or an issue URL.
4. Tap Dispatch (per row) or Dispatch next (per repo group).

No config files, no registration step. If the repo is missing from
`/api/repos`, the checkout (or its `.git`) is absent — that is the
only onboarding failure mode.

## What dispatch checks (per-repo gates)

Gates are read live at every dispatch — never assumed, never
cached. A blocked dispatch names each failing gate:

- **Checkout**: the repo has a discovered checkout and it is clean
  (uncommitted changes block, so the worker never builds on a
  dirty tree).
- **Repo idle**: no other lane item is running for the same repo
  (per-repo conflict detection; other repos dispatch freely).
- **Worker idle**: the repair worker runs one item at a time
  globally; a running item blocks every repo.
- **Verify allowlisted**: the issue's verification command parses
  and is allowlisted. An explicitly rejected command blocks; an
  issue with no verification command stays dispatchable and the
  worker reports its own verdict.
- **Protection**: the repo's default branch + required status
  checks, read from branch protection at dispatch. Required checks
  differ per repo (tech-dancer's rulesets are not remote-server's).
  An unprotected branch is an empty bar — still dispatchable.

Tap Check gates on any queued row to preview its gates without
dispatching.

## Execution + monitoring

Dispatch launches the repo-scoped repair worker
(`python3 -m backend.repair_worker --issue N --repo R`) — the same
branch/PR flow the single-repo console runs. A monitor watches the
worker and records the item `done`/`failed` when it settles
(timeout: 35 minutes, then failed). Terminal items stay in the
queue for retry (Re-dispatch) or removal (Remove).

The repair worker stays globally single-flight: queue depth across
repos is fine, but only one item runs at a time. Running items
refuse Remove — abort through the repair worker, which owns the
proc.

## API

- `GET /api/dispatch/queue` — every item, oldest first.
- `GET /api/dispatch/gates?repo=R&issue=N` — live readiness.
- `POST /api/dispatch/enqueue` `{repo, issue}` — idempotent.
- `POST /api/dispatch/start` `{repo, issue, intent:
  "dispatch-start", confirmed: true}` — 409 with gate reasons when
  blocked.
- `POST /api/dispatch/cancel` `{repo, issue}` — drops a
  queued/terminal item; 409 while running.

## Scope fence (parallel lanes)

The queue owns only: `backend/lane_dispatch.py`,
`backend/api/routers/dispatch.py`, the Queue console view
(`QueueView`, `queueUtils`, dispatch API client calls), this
runbook, and their tests. It must not touch Review/pulls, notify,
`backend/review.py`, AgentDrawer, e2e, or the webhook pull path.
