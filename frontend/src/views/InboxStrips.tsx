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
} from './ReviewView';


// Swipe-to-act threshold for inbox strips (issue #403): a
// horizontal touch drag past this distance fires the row action;
// shorter or mostly-vertical drags are taps/scrolls, untouched.
export const SWIPE_ACT_PX = 64;

// Busy-guard feedback (issue #1010): a swipe or undo landing while a
// row update is in flight must surface a notice — silently dropping
// it leaves the row reading dead. Null when idle (no notice).
export const STRIPS_BUSY_NOTICE =
  'A row update is already in progress — try again in a moment.';

export function stripsBusyNotice(
  busyOp: 'defer' | 'restore' | null,
): string | null {
  return busyOp === null ? null : STRIPS_BUSY_NOTICE;
}

// Pure swipe classifier so the gesture rule is unit-testable
// without a touch device: dx/dy in CSS pixels from touchstart to
// touchend. Null means "not a swipe" — leave the tap alone.
export function swipeDirection(dx: number, dy: number): 'left' | 'right' | null {
  if (Math.abs(dx) < SWIPE_ACT_PX) return null;
  if (Math.abs(dy) > Math.abs(dx) / 2) return null;
  return dx < 0 ? 'left' : 'right';
}

// Touch-only swipe wrapper for one strip row. Taps
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
// per-item defer/restore actions (#1317).
// Deferred flags hide rows from the merge-ready list (Review parity)
// and surface them in the deferred section instead. The parent owns
// the deferred flags; dispatch lives here.
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
  const [busyOp, setBusyOp] = useState<'defer' | 'restore' | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [undo, setUndo] = useState<{ op: 'defer' | 'restore'; rows: RowRef[] } | null>(null);
  const ready = feed.merge_ready;
  const active = feed.runs.active;
  const visibleTop = ready.top.filter((p) => !isRowDeferred(deferred, 'pr', p.number));
  if (visibleTop.length === 0 && !active && deferred.length === 0) return null;

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


  // Single-row swipe op (issue #403): same dispatch as bulk, plus
  // an undo snapshot so a swipe-dismiss is reversible from the
  // notice. Merge-ready rows swipe left to defer (dismiss) and
  // right to open Review (primary action); deferred rows swipe
  // either way to restore.
  async function runSingle(op: 'defer' | 'restore', rows: RowRef[]) {
    const busyNotice = stripsBusyNotice(busyOp);
    if (busyNotice) {
      setError(busyNotice);
      return;
    }
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
    if (!undo) return;
    const busyNotice = stripsBusyNotice(busyOp);
    if (busyNotice) {
      setError(busyNotice);
      return;
    }
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
      {active && (
        <div className="actions">
          <button type="button" data-testid="inbox-active-run" onClick={onOpenJobs}>
            Active: {active.action}
          </button>
        </div>
      )}
      {(visibleTop.length > 0 || deferred.length > 0) && (
        <>
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
                  {busyOp !== null ? 'Updating…' : 'Undo'}
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
                <span className="row-title">
                  #{p.number} {p.title}
                </span>
                <button type="button" onClick={onOpenReview}>Review</button>
                <button type="button" onClick={() => void runSingle('defer', [{ kind: 'pr', number: p.number }])}>Defer</button>
              </SwipeRow>
            ))}
            {deferred.map((d) => (
              <SwipeRow
                key={`${d.kind}-${d.number}`}
                testId={`strips-row-${d.kind}-${d.number}`}
                onSwipe={() => void runSingle('restore', [{ kind: d.kind, number: d.number }])}
              >
                <span className="row-title">
                  #{d.number} {d.kind}
                </span>
                <button type="button" onClick={() => void runSingle('restore', [{ kind: d.kind, number: d.number }])}>Restore</button>
              </SwipeRow>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
