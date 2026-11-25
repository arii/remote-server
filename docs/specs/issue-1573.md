## Problem Statement
A running lane-queue item cannot be stopped from the console. `queueRowActions` in `frontend/src/views/queueUtils.ts` returns `{dispatch: false, cancel: false}` for `running`, so the row renders no action at all, and `lane_dispatch.cancel` (`backend/lane_dispatch.py`) refuses running items with `"item is running"` (surfaced as a 409 by `POST /api/dispatch/cancel` in `backend/api/routers/dispatch.py`). The only way to stop a runaway run today is to know that `POST /api/repair/abort` exists (`backend/api/routers/issues.py`) and call it out-of-band — and even then the queue item keeps showing `running` until the monitor notices, the single-flight worker claim is not visibly released as an operator action, and nothing records that a human cancelled the run. A runaway run therefore holds the repo lane hostage with no operator recourse.

## Goal
A running item gets a Cancel action with confirm that (1) aborts the in-flight repair worker through the existing `POST /api/repair/abort` path, (2) flips the item out of `running` and records `cancelled-by-operator` as the terminal detail, releasing the lane claim, and (3) gives the operator a visible choice of what happens to the issue afterwards — return to ready or park — and never leaves the item in a claimed/limbo state.

## Scope & File Boundaries
### Allowed Files (Create / Modify / Delete)
- `backend/lane_dispatch.py` — `cancel_running(repo, issue, disposition)`: abort-aware cancel that marks the item terminal with `detail` recording `cancelled-by-operator` plus the chosen disposition (`"ready"` or `"parked"`), in one locked mutation.
- `backend/api/routers/dispatch.py` — extend `POST /api/dispatch/cancel` (or add `/cancel-running`) to accept `{repo, issue, disposition, confirmed: true}` for running items, orchestrating: call the repair abort, then the lane state mutation. Reuse `require_confirm` from `backend/core/validation.py`.
- `frontend/src/views/QueueView.tsx` — running rows show a Cancel button that opens confirm + the ready/park choice.
- `frontend/src/views/queueUtils.ts` — `queueRowActions` returns a cancel action for `running`.
- `backend/tests/test_lane_dispatch.py`, `frontend/src/views/queueUtils.test.ts`, `frontend/src/views/QueueView.test.tsx` — coverage.

### Forbidden Files
- `deep-agent/coder.py`
- `scripts/queue_runner.py` (the TwT queue lane is a separate system)

## Implementation Plan
1. `backend/lane_dispatch.py`: `cancel_running(repo, issue, disposition)` — under `_lock`, require `state == "running"` (else return error so a stale row cannot double-cancel), set terminal state `failed` with `finished_at`, `detail = "cancelled-by-operator; issue returns to ready"` or `"cancelled-by-operator; issue parked"`, persist atomically.
2. `backend/api/routers/dispatch.py`: for a running item, the cancel endpoint (a) invokes the repair-worker abort (the same logic as `POST /api/repair/abort` in `backend/api/routers/issues.py` — factor the flag+terminate sequence into a shared helper rather than duplicating it), then (b) applies `cancel_running`. Ordering matters: abort first so the monitor cannot race the manual mutation and overwrite the detail; the monitor already treats a non-running worker as terminal and must not overwrite a `cancelled-by-operator` detail (guard: monitor skips items no longer in `running`).
3. Disposition surfacing: the choice is recorded in `detail`; if the lane currently reflects issue disposition anywhere operator-visible (the console row meta), render it there.
4. Frontend: running rows show Cancel; clicking opens an inline confirm with two radio choices — "Return to ready" / "Park" — matching the same confirm UX pattern as the retry flow (#1572).

## Definition of Done
* A running item can be cancelled from the console with confirm; the worker proc is terminated, the item leaves `running` immediately, and the detail records cancelled-by-operator plus disposition.
* The lane claim is released: another item for the same repo can dispatch right after the cancel (no stuck `worker_busy` / `repo_idle` gate).
* The item never stays claimed or half-running: even if the abort kill fails, the state mutation still lands and the detail says so.

## Acceptance Criteria
- [ ] Cancel of a running item without `confirmed: true` is rejected.
- [ ] After cancel, `GET /api/dispatch/queue` shows the item terminal with `cancelled-by-operator` in detail and the chosen disposition.
- [ ] `readiness` for the repo no longer reports the lane busy right after cancel.
- [ ] The monitor thread does not overwrite the operator-cancel detail.
- [ ] Console row for a running item shows Cancel with confirm + the ready/park choice.

## Verification Commands
```bash
python3 -m pytest backend/tests/test_lane_dispatch.py -x -q
cd frontend && pnpm exec vitest run src/views/queueUtils.test.ts src/views/QueueView.test.tsx
```

## Blocking questions
* Whether "returns to ready" should also touch the GitHub `ready` label: the lane queue itself is manual-dispatch, so the disposition is recorded on the item; if the TwT queue's `ready` label is meant, that is follow-up wiring, not this issue. Assumed: item-level disposition only.
* Exact factoring of the abort helper: it must not double-terminate when the operator cancels an item whose worker already exited — mirror the existing `repair_abort` poll check.

## Investigation Evidence
- `frontend/src/views/queueUtils.ts` `queueRowActions`: `if (state === 'running') return { dispatch: false, cancel: false }` — running rows render no action.
- `backend/lane_dispatch.py` `cancel`: `if item.get("state") == RUNNING: return False, "item is running"` — surfaced as 409 in `backend/api/routers/dispatch.py` (`if err == "item is running": raise HTTPException(status_code=409 ...)`), with the comment "abort it through the repair worker, which owns the proc".
- `backend/api/routers/issues.py` `POST /api/repair/abort` (line ~946): sets `_repair_abort` flag, terminates the proc with a kill fallback, worker records status `"aborted"` — the abort mechanism exists but is not wired to the lane queue.
- `backend/lane_dispatch.py` `_monitor_item`: the monitor writes terminal state only `if item.get("state") == RUNNING` — so an item flipped out of running by the operator cancel is naturally protected, provided the cancel mutation lands before or atomically with the abort.
