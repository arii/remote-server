import { useState } from 'react';
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
  const ready = feed.merge_ready;
  const active = feed.runs.active;
  const visibleTop = ready.top.filter((p) => !isRowDeferred(deferred, 'pr', p.number));
  const topKeys = visibleTop.map((p) => bulkRowKey('pr', p.number));
  const deferredKeys = deferred.map((d) => bulkRowKey(d.kind, d.number));
  const allKeys = [...topKeys, ...deferredKeys];
  if (ready.count === 0 && !active && deferred.length === 0) return null;

  async function runBulk(op: 'defer' | 'restore') {
    const rows = parseBulkSelection(selected);
    if (rows.length === 0) return;
    setError(null);
    setDone(null);
    setBusyOp(op);
    try {
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
      setSelected(new Set());
      setDone(op === 'defer' ? bulkDeferNotice(rows.length) : bulkRestoreNotice(rows.length));
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
              {done}
            </p>
          )}
          <ul className="session-list" data-testid="strips-rows">
            {visibleTop.map((p) => (
              <li key={`pr-${p.number}`} data-testid={`strips-row-pr-${p.number}`}>
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
              </li>
            ))}
            {deferred.map((d) => (
              <li key={`${d.kind}-${d.number}`} data-testid={`strips-row-${d.kind}-${d.number}`}>
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
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
