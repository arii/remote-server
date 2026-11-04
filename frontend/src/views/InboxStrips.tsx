import { useRef, useState } from 'react';
import type { ReactNode, TouchEvent } from 'react';
import { deferRowsBulk } from '../apiClient';
import type { InboxFeed } from '../apiClient';
import type { DeferredRow } from '../reviewTypes';
import { useMock } from './mockViews';
import {
  bulkDeferNotice,
  bulkRestoreNotice,
  bulkRowKey,
  isRowDeferred,
  parseBulkSelection,
  toggleBulkSelectAll,
  toggleBulkSelected,
} from './ReviewView';

// Bulk bar for the inbox strips: one bar, both actions. Mixed
// selections are safe — the bulk endpoint no-ops rows its op
// doesn't apply to (re-defer / restore-absent).
export function StripsBulkBar({
  count,
  busyOp,
  onDefer,
  onRestore,
}: {
  count: number;
  busyOp: 'defer' | 'restore' | null;
  onDefer: () => void;
  onRestore: () => void;
}) {
  return (
    <div className="actions" data-testid="strips-bulk-bar" role="status">
      {count} selected{' '}
      <button
        type="button"
        data-testid="strips-bulk-defer"
        aria-label={`Defer ${count} selected row${count === 1 ? '' : 's'}`}
        disabled={busyOp !== null}
        onClick={onDefer}
      >
        {busyOp === 'defer' ? 'Deferring…' : `Defer ${count}`}
      </button>{' '}
      <button
        type="button"
        data-testid="strips-bulk-restore"
        aria-label={`Restore ${count} selected row${count === 1 ? '' : 's'}`}
        disabled={busyOp !== null}
        onClick={onRestore}
      >
        {busyOp === 'restore' ? 'Restoring…' : `Restore ${count}`}
      </button>
    </div>
  );
}

// Swipe-to-act threshold for inbox strips (issue #403): a
// horizontal touch drag past this distance fires the row action;
// shorter or mostly-vertical drags are taps/scrolls, untouched.
export const SWIPE_ACT_PX = 64;

// Pure swipe classifier so the gesture rule is unit-testable
// without a touch device: dx/dy in CSS pixels from touchstart to
// touchend. Null means "not a swipe" — leave the tap alone.
export function swipeDirection(dx: number, dy: number): 'left' | 'right' | null {
  if (Math.abs(dx) < SWIPE_ACT_PX) return null;
  if (Math.abs(dy) > Math.abs(dx) / 2) return null;
  return dx < 0 ? 'left' : 'right';
}

// Touch-only swipe wrapper for one strip row. Taps, checkboxes,
// and keyboard focus are untouched: the action fires only on a
// committed horizontal drag past SWIPE_ACT_PX, and no default is
// prevented, so scrolling and tap activation behave as before.
function SwipeRow({
  testId,
  onSwipe,
  children,
}: {
  testId: string;
  onSwipe: (dir: 'left' | 'right') => void;
  children: ReactNode;
}) {
  const start = useRef<{ x: number; y: number } | null>(null);
  const [offset, setOffset] = useState(0);
  return (
    <li
      data-testid={testId}
      style={offset !== 0 ? { transform: `translateX(${offset}px)` } : undefined}
      onTouchStart={(e: TouchEvent) => {
        const t = e.touches[0];
        start.current = { x: t.clientX, y: t.clientY };
        setOffset(0);
      }}
      onTouchMove={(e: TouchEvent) => {
        const s = start.current;
        if (!s) return;
        const t = e.touches[0];
        const dx = t.clientX - s.x;
        const dy = t.clientY - s.y;
        if (Math.abs(dx) < 8 || Math.abs(dy) > Math.abs(dx)) {
          setOffset(0);
          return;
        }
        setOffset(Math.max(-96, Math.min(96, dx)));
      }}
      onTouchEnd={(e: TouchEvent) => {
        const s = start.current;
        start.current = null;
        setOffset(0);
        if (!s) return;
        const t = e.changedTouches[0];
        const dir = swipeDirection(t.clientX - s.x, t.clientY - s.y);
        if (dir) onSwipe(dir);
      }}
    >
      {children}
    </li>
  );
}

type RowRef = { kind: 'pr' | 'issue'; number: number };

// Compact actionable strips between the attention feed and the
// Review/Jobs sections: merge-ready PRs and the active run, plus
// selectable inbox rows with a bulk defer/restore bar (issue #591).
// Deferred flags hide rows from the merge-ready list (Review parity)
// and surface them in the deferred section instead. The parent owns
// the deferred flags; selection and dispatch live here.
export default function InboxStrips({
  feed,
  repo,
  deferred,
  onDeferredChange,
  onOpenReview,
  onOpenJobs,
}: {
  feed: InboxFeed;
  repo: string;
  deferred: DeferredRow[];
  onDeferredChange: (rows: DeferredRow[]) => void;
  onOpenReview: () => void;
  onOpenJobs: () => void;
}) {
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [busyOp, setBusyOp] = useState<'defer' | 'restore' | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [undo, setUndo] = useState<{ op: 'defer' | 'restore'; rows: RowRef[] } | null>(null);
  const ready = feed.merge_ready;
  const active = feed.runs.active;
  const visibleTop = ready.top.filter((p) => !isRowDeferred(deferred, 'pr', p.number));
  const topKeys = visibleTop.map((p) => bulkRowKey('pr', p.number));
  const deferredKeys = deferred.map((d) => bulkRowKey(d.kind, d.number));
  const allKeys = [...topKeys, ...deferredKeys];
  if (ready.count === 0 && !active && deferred.length === 0) return null;

  async function applyOp(op: 'defer' | 'restore', rows: RowRef[]): Promise<void> {
    if (useMock()) {
      if (op === 'defer') {
        const have = new Set(deferred.map((d) => bulkRowKey(d.kind, d.number)));
        const fresh = rows.filter((r) => !have.has(bulkRowKey(r.kind, r.number)));
        onDeferredChange([
          ...deferred,
          ...fresh.map((r) => ({ ...r, deferred_at: Date.now() / 1000 })),
        ]);
      } else {
        const drop = new Set(rows.map((r) => bulkRowKey(r.kind, r.number)));
        onDeferredChange(deferred.filter((d) => !drop.has(bulkRowKey(d.kind, d.number))));
      }
    } else {
      const r = await deferRowsBulk(repo, op, rows);
      onDeferredChange(r.deferred);
    }
  }

  async function runBulk(op: 'defer' | 'restore') {
    const rows = parseBulkSelection(selected);
    if (rows.length === 0) return;
    setError(null);
    setDone(null);
    setUndo(null);
    setBusyOp(op);
    try {
      await applyOp(op, rows);
      setSelected(new Set());
      setDone(op === 'defer' ? bulkDeferNotice(rows.length) : bulkRestoreNotice(rows.length));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyOp(null);
    }
  }

  // Single-row swipe op (issue #403): same dispatch as bulk, plus
  // an undo snapshot so a swipe-dismiss is reversible from the
  // notice. Merge-ready rows swipe left to defer (dismiss) and
  // right to open Review (primary action); deferred rows swipe
  // either way to restore.
  async function runSingle(op: 'defer' | 'restore', rows: RowRef[]) {
    if (busyOp !== null) return;
    setError(null);
    setDone(null);
    setUndo(null);
    setBusyOp(op);
    try {
      await applyOp(op, rows);
      setUndo({ op, rows });
      setDone(op === 'defer' ? bulkDeferNotice(rows.length) : bulkRestoreNotice(rows.length));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyOp(null);
    }
  }

  async function runUndo() {
    if (!undo || busyOp !== null) return;
    const back = undo.op === 'defer' ? 'restore' : 'defer';
    const rows = undo.rows;
    setError(null);
    setDone(null);
    setUndo(null);
    setBusyOp(back);
    try {
      await applyOp(back, rows);
      setDone(back === 'defer' ? bulkDeferNotice(rows.length) : bulkRestoreNotice(rows.length));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyOp(null);
    }
  }

  return (
    <div data-testid="inbox-strips">
      <div className="actions">
        {ready.count > 0 && (
          <button type="button" data-testid="inbox-merge-ready" onClick={onOpenReview}>
            Ready to merge ({ready.count}
            {ready.top.length > 0 ? `): ${ready.top.map((p) => `#${p.number} ${p.title}`).join(', ')}` : ')'}
          </button>
        )}
        {active && (
          <button type="button" data-testid="inbox-active-run" onClick={onOpenJobs}>
            Active: {active.action}
          </button>
        )}
      </div>
      {allKeys.length > 0 && (
        <>
          <label className="row-meta">
            <input
              type="checkbox"
              data-testid="strips-select-all"
              aria-label="Select all inbox rows"
              checked={allKeys.every((k) => selected.has(k))}
              onChange={() => setSelected((prev) => toggleBulkSelectAll(prev, allKeys))}
            />{' '}
            all
          </label>
          {selected.size > 0 && (
            <StripsBulkBar
              count={selected.size}
              busyOp={busyOp}
              onDefer={() => void runBulk('defer')}
              onRestore={() => void runBulk('restore')}
            />
          )}
          {error && (
            <p role="alert" data-testid="strips-error">
              Couldn&apos;t update rows: {error}
            </p>
          )}
          {done && (
            <p className="row-meta" data-testid="strips-done" role="status">
              {done}{' '}
              {undo && (
                <button
                  type="button"
                  data-testid="strips-undo"
                  disabled={busyOp !== null}
                  onClick={() => void runUndo()}
                >
                  Undo
                </button>
              )}
            </p>
          )}
          <ul className="session-list" data-testid="strips-rows">
            {visibleTop.map((p) => (
              <SwipeRow
                key={`pr-${p.number}`}
                testId={`strips-row-pr-${p.number}`}
                onSwipe={(dir) => {
                  if (dir === 'left') void runSingle('defer', [{ kind: 'pr', number: p.number }]);
                  else onOpenReview();
                }}
              >
                <input
                  type="checkbox"
                  data-testid={`strips-row-check-pr-${p.number}`}
                  aria-label={`Select PR #${p.number}`}
                  checked={selected.has(bulkRowKey('pr', p.number))}
                  onChange={() => setSelected((prev) => toggleBulkSelected(prev, bulkRowKey('pr', p.number)))}
                />{' '}
                <span className="row-title">
                  #{p.number} {p.title}
                </span>
              </SwipeRow>
            ))}
            {deferred.map((d) => (
              <SwipeRow
                key={`${d.kind}-${d.number}`}
                testId={`strips-row-${d.kind}-${d.number}`}
                onSwipe={() => void runSingle('restore', [{ kind: d.kind, number: d.number }])}
              >
                <input
                  type="checkbox"
                  data-testid={`strips-row-check-${d.kind}-${d.number}`}
                  aria-label={`Select ${d.kind} #${d.number}`}
                  checked={selected.has(bulkRowKey(d.kind, d.number))}
                  onChange={() => setSelected((prev) => toggleBulkSelected(prev, bulkRowKey(d.kind, d.number)))}
                />{' '}
                <span className="row-title">
                  #{d.number} {d.kind}
                </span>
              </SwipeRow>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
