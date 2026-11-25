# Issue 1569: Backgrounded phone shows stale queue state as if live

## Problem Statement
In `frontend/src/views/QueueView.tsx#L451-L465`, the `onTabVisible` listener is bound within an effect that early-exits when `!items || !queueListActive(items)` is true. If the queue is empty or has only completed or idle items, `queueListActive(items)` is false, preventing `onTabVisible` from registering. Consequently, when a mobile browser tab is backgrounded and later resumed, `QueueView` does not refetch fresh queue state and continues showing stale data. Additionally, the view lacks a freshness indicator (updated-Xs-ago) and does not display a banner when refetch fails.

## Goal
Bind `onTabVisible` unconditionally in `QueueView` so returning to the tab always triggers a refresh, display an updated-Xs-ago freshness indicator in `QueueHeader`, and display a cannot-reach-server banner if a refetch fails.

## Scope & File Boundaries
### Allowed Files (Create / Modify / Delete)
- `docs/specs/issue-1569.md` (CREATE)
- `frontend/src/views/QueueView.tsx`
- `frontend/src/views/QueueView.test.tsx`

### Forbidden Files
- `backend/server.py`
- `docker-compose.yml`
- `deep-agent/coder.py`

## Implementation Plan
1. In `frontend/src/views/QueueView.tsx`, register `onTabVisible(() => void guarded(load))` in an effect independent of `queueListActive(items)`, ensuring visibility resumption always triggers `load()`.
2. Add `lastUpdated` timestamp state and `reachError` state to `QueueView`.
3. In `load()`, record `Date.now()` on successful fetch and clear `reachError`. In `catch`, record `reachError = true`.
4. Update `QueueHeader` to accept `lastUpdated` and `reachError`, rendering "Updated Xs ago" in row-meta and a warning banner when `reachError` is active.
5. In `frontend/src/views/QueueView.test.tsx`, add test cases asserting `onTabVisible` refetches on idle queue, "Updated" freshness is rendered, and reachability warning displays on failure.

## Definition of Done
- `onTabVisible` registers unconditionally on mount.
- `QueueHeader` renders freshness indicator and handles server-reach error states.
- Vitest suite in `frontend/src/views/QueueView.test.tsx` passes with new assertions.

## Acceptance Criteria
- [ ] `onTabVisible` triggers `load()` even when `queueListActive(items)` is false.
- [ ] Freshness timestamp ("Updated") is rendered in `QueueHeader`.
- [ ] Server reachability failure renders a warning banner.
- [ ] All vitest test suites in `frontend/src/views/QueueView.test.tsx` pass.

## Verification Commands
```bash
pnpm --filter frontend test -- QueueView.test.tsx
```
