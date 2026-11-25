## Problem Statement
A failed lane-queue item today shows a generic "Re-dispatch" button (`frontend/src/views/QueueView.tsx`, `queueRowActions` in `frontend/src/views/queueUtils.ts` returns `dispatch: true` for terminal states). Tapping it goes straight through `POST /api/dispatch/start` with no confirm and no choice of retry mode: the repair worker always creates a fresh timestamped branch (`backend/repair_worker.py` line ~350: `repair/issue-<N>-<int(time.time())>`), so any work already committed on the previous attempt's branch is silently abandoned. There is also no server-side protection against a double tap: two fast taps both reach `lane_dispatch.dispatch` before the item flips state, which can spawn duplicate work or a second PR for the same issue.

## Goal
One explicit Retry action on a failed item that (1) asks the operator to choose fresh-branch vs resume-the-existing-branch, (2) requires confirm, and (3) flips the item to `queued` atomically server-side at retry-request time, so a second tap is a no-op rather than a duplicate dispatch.

## Scope & File Boundaries
### Allowed Files (Create / Modify / Delete)
- `backend/lane_dispatch.py` — retry semantics: `retry(repo, issue, mode)` that validates the item is `failed`, records the mode, and atomically flips the item to `queued` in one locked mutation (returns the existing item unchanged if it is no longer `failed` — that is the double-tap guard).
- `backend/api/routers/dispatch.py` — `POST /api/dispatch/retry` with `{repo, issue, mode: "fresh"|"resume", confirmed: true}`; uses the same `require_confirm` pattern as `/start` (`backend/core/validation.py`).
- `backend/repair_worker.py` — when the dispatch carries `mode: "resume"` and a `branch_name` for a previous attempt exists, reuse that branch for the worktree instead of minting `repair/issue-<N>-<timestamp>`; `fresh` keeps today's behavior.
- `frontend/src/views/QueueView.tsx` — replace the terminal-state "Re-dispatch" button with a "Retry" action that opens the mode choice (fresh-branch vs resume) and confirm before calling the retry endpoint.
- `frontend/src/views/queueUtils.ts` — row action metadata for the retry action.
- `frontend/src/views/queueUtils.test.ts`, `frontend/src/views/QueueView.test.tsx` — UI coverage.
- `backend/tests/test_lane_dispatch.py` — retry state-machine coverage.

### Forbidden Files
- `deep-agent/coder.py`
- `scripts/queue_runner.py` (the TwT queue lane is a separate system; this issue is the console lane queue)

## Implementation Plan
1. `backend/lane_dispatch.py`: add `retry(repo, issue, mode)` — under `_lock`, load, require `state == "failed"` (anything else returns the unchanged item, making a second tap idempotent), set `state = "queued"`, clear `finished_at`, record `detail = "retry queued (mode)"` and persist in the same locked mutation as `_persist_locked()`.
2. `backend/api/routers/dispatch.py`: `POST /api/dispatch/retry` validating repo/issue/mode with the same seams as `/start` (`parse_issue_number`, `validate_repo_name`, `require_confirm`); invalid mode → 400; unknown item → 404.
3. `backend/repair_worker.py`: accept an optional resume branch; when resuming, `git worktree add` from the existing branch instead of `-b` a new one.
4. `lane_dispatch.dispatch` passes the recorded mode/branch through to the repair worker launch.
5. Frontend: `Retry` button on failed items opens a small inline confirm with two radio choices (fresh branch / resume existing branch); on confirm, POST retry, then the row re-renders as queued.

## Definition of Done
* Failed item shows exactly one Retry action; choosing a mode + confirm flips it to queued immediately.
* A second retry tap on the same item cannot duplicate or open a second PR (server-side idempotency, not just UI disabling).
* Resume mode reuses the previous attempt's branch; fresh mode mints a new timestamped branch.

## Acceptance Criteria
- [ ] `POST /api/dispatch/retry` without `confirmed: true` is rejected (same confirm contract as `/start`).
- [ ] Retrying a `failed` item flips it to `queued` in one server-side mutation; a concurrent/duplicate retry returns the unchanged item, never a second dispatch.
- [ ] `mode: "resume"` reuses the prior `branch_name`; `mode: "fresh"` mints `repair/issue-<N>-<ts>`.
- [ ] The console row for a failed item shows one Retry button with the mode choice + confirm, and no plain "Re-dispatch" passthrough.

## Verification Commands
```bash
python3 -m pytest backend/tests/test_lane_dispatch.py -x -q
cd frontend && pnpm exec vitest run src/views/queueUtils.test.ts src/views/QueueView.test.tsx
```

## Blocking questions
* Does `lane_dispatch.dispatch` need a schema change to carry mode/branch, or is the mode stored on the item dict (preferred: persisted with the item so a console restart does not lose it)? Assumed: stored on the item.
* Resume when the previous branch was already merged or deleted: fall back to fresh with a visible note in `detail` (do not silently fail).

## Investigation Evidence
- `frontend/src/views/queueUtils.ts` `queueRowActions`: terminal states return `{dispatch: true, cancel: true}` — the failed-item button is the generic dispatch path.
- `frontend/src/views/QueueView.tsx` `QueueRow`: button label is "Re-dispatch" for non-queued states, wired to `onDispatch` → `POST /api/dispatch/start` (no mode, confirm lives only on `/start`'s intent check).
- `backend/repair_worker.py` line ~350: `branch_name = state.get("branch_name") or f"repair/issue-{issue_number}-{int(time.time())}"` — a stored `branch_name` is already honored when present, so resume is a matter of passing it through.
- `backend/lane_dispatch.py` `enqueue` docstring already calls terminal re-queue "(retry)" but there is no mode concept and no atomic flip distinct from dispatch.
