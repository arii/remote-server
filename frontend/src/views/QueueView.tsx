import { useCallback, useEffect, useState } from 'react';
import {
  cancelDispatch,
  enqueueDispatch,
  fetchDispatchGates,
  fetchDispatchQueue,
  startDispatch,
} from '../apiClient';
import type { DispatchItem } from '../apiTypes';
import { isTabVisible, onTabVisible } from './jobsUtils';
import type { DispatchRepoGroup } from './queueUtils';
import {
  groupQueueByRepo,
  nextDispatchable,
  queueListActive,
  queueRowActions,
  queueRowMeta,
  splitBlockReasons,
} from './queueUtils';
import { loadDispatchQueue, useMock } from './mockViews';

function QueueHeader({
  count,
  live,
  onRefresh,
}: {
  count: number;
  live: boolean;
  onRefresh: () => void;
}) {
  return (
    <header className="page-head">
      <div>
        <h3>Lane queue</h3>
        <p className="row-meta">
          {count} item{count === 1 ? '' : 's'}
          {live ? ' · Live' : ''}
        </p>
      </div>
      <button type="button" data-testid="queue-refresh" onClick={onRefresh}>
        Refresh
      </button>
    </header>
  );
}

export function QueueRow({
  item,
  gates,
  onDispatch,
  onCancel,
  onGates,
}: {
  item: DispatchItem;
  gates: string[] | null;
  onDispatch: (item: DispatchItem) => void;
  onCancel: (item: DispatchItem) => void;
  onGates: (item: DispatchItem) => void;
}) {
  const actions = queueRowActions(item.state);
  return (
    <li
      key={`${item.repo}#${item.issue}`}
      className="detail"
      data-testid={`queue-${item.repo}-${item.issue}`}
    >
      <span className="row-title">
        {item.repo}#{item.issue}
      </span>
      <p className="row-meta">{queueRowMeta(item)}</p>
      {gates && gates.length > 0 && (
        <ul className="row-meta" data-testid={`queue-gates-${item.repo}-${item.issue}`}>
          {gates.map((g, i) => (
            <li key={i}>{g}</li>
          ))}
        </ul>
      )}
      <div className="actions">
        {actions.dispatch && (
          <button
            type="button"
            data-testid="queue-dispatch"
            onClick={() => onDispatch(item)}
          >
            {item.state === 'queued' ? 'Dispatch' : 'Re-dispatch'}
          </button>
        )}{' '}
        {item.state === 'queued' && (
          <button type="button" data-testid="queue-gates-check" onClick={() => onGates(item)}>
            Check gates
          </button>
        )}{' '}
        {actions.cancel && (
          <button type="button" data-testid="queue-cancel" onClick={() => onCancel(item)}>
            Remove
          </button>
        )}
      </div>
    </li>
  );
}

export function QueueRepoGroup({
  group,
  gatesByKey,
  onDispatch,
  onDispatchNext,
  onCancel,
  onGates,
}: {
  group: DispatchRepoGroup;
  gatesByKey: Record<string, string[] | null>;
  onDispatch: (item: DispatchItem) => void;
  onDispatchNext: (group: DispatchRepoGroup) => void;
  onCancel: (item: DispatchItem) => void;
  onGates: (item: DispatchItem) => void;
}) {
  const next = nextDispatchable(group);
  return (
    <section aria-label={`Queue for ${group.repo}`}>
      <h3>{group.repo}</h3>
      <div className="actions">
        <button
          type="button"
          data-testid={`queue-dispatch-next-${group.repo}`}
          disabled={!next}
          title={next ? `Dispatch ${group.repo}#${next.issue}` : 'Nothing queued'}
          onClick={() => onDispatchNext(group)}
        >
          Dispatch next
        </button>
      </div>
      <ul className="session-list" data-testid={`queue-group-${group.repo}`}>
        {group.items.map((item) => (
          <QueueRow
            key={`${item.repo}#${item.issue}`}
            item={item}
            gates={gatesByKey[`${item.repo}#${item.issue}`] ?? null}
            onDispatch={onDispatch}
            onCancel={onCancel}
            onGates={onGates}
          />
        ))}
      </ul>
    </section>
  );
}

// Cross-repo lane dispatch queue (issue #853): one worklist keyed
// by (repo, issue), grouped by repo with per-repo dispatch taps.
// Jobs patterns throughout: header + live poll-while-active gated
// on tab visibility, notice/error lines, row actions.
export default function QueueView({ repo }: { repo: string }) {
  const [items, setItems] = useState<DispatchItem[] | null>(null);
  const [issue, setIssue] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [gatesByKey, setGatesByKey] = useState<Record<string, string[] | null>>({});

  async function guarded(fn: () => Promise<void>) {
    setError(null);
    try {
      await fn();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  const load = useCallback(async () => {
    if (useMock()) {
      setItems(loadDispatchQueue().items);
      return;
    }
    const live = await fetchDispatchQueue();
    setItems(live.items);
  }, []);

  useEffect(() => {
    void guarded(load);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load]);

  // Fallback list poll while any item is live, paused in a hidden
  // tab; a returning tab refreshes immediately (Jobs pattern).
  useEffect(() => {
    if (useMock()) return;
    if (!items || !queueListActive(items)) return;
    const timer = window.setInterval(() => {
      if (isTabVisible()) void guarded(load);
    }, 5000);
    const stopResume = onTabVisible(() => void guarded(load));
    return () => {
      window.clearInterval(timer);
      stopResume();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items, load]);

  async function onEnqueue() {
    const n = issue.trim().replace(/^#+/, '');
    if (!/^\d+$/.test(n)) {
      setError('Enter an issue number first.');
      return;
    }
    const row = await enqueueDispatch(repo, n);
    setIssue('');
    setNotice(`Queued ${row.repo}#${row.issue}.`);
    await load();
  }

  async function onDispatch(item: DispatchItem) {
    try {
      await startDispatch(item.repo, item.issue);
    } catch (err) {
      // 409 carries the joined gate reasons: surface each line
      // instead of the raw status line (Jobs stale-row notices).
      const message = err instanceof Error ? err.message : String(err);
      const detail = message.split(' 409 ').slice(1).join(' 409 ').trim();
      const reasons = detail ? splitBlockReasons(detail) : [];
      setNotice(
        reasons.length > 0
          ? `Blocked ${item.repo}#${item.issue}: ${reasons.join(' · ')}`
          : `Couldn't dispatch ${item.repo}#${item.issue}: ${message}`,
      );
      await load();
      return;
    }
    setNotice(`Dispatched ${item.repo}#${item.issue}.`);
    await load();
  }

  function onDispatchNext(group: DispatchRepoGroup) {
    const next = nextDispatchable(group);
    if (next) void guarded(() => onDispatch(next));
  }

  async function onCancel(item: DispatchItem) {
    await cancelDispatch(item.repo, item.issue);
    setNotice(`Removed ${item.repo}#${item.issue}.`);
    await load();
  }

  async function onGates(item: DispatchItem) {
    const key = `${item.repo}#${item.issue}`;
    const g = await fetchDispatchGates(item.repo, item.issue);
    setGatesByKey((prev) => ({
      ...prev,
      [key]: g.ready ? ['Ready to dispatch.'] : g.reasons,
    }));
  }

  if (!items && !error)
    return (
      <section data-testid="queue-loading" role="status">
        Loading…
      </section>
    );

  const list = items ?? [];
  const groups = groupQueueByRepo(list);

  return (
    <section data-testid="queue-view">
      <QueueHeader
        count={list.length}
        live={queueListActive(list)}
        onRefresh={() => void guarded(load)}
      />
      {error && (
        <p role="alert" data-testid="queue-error">
          Couldn&apos;t load the queue: {error}
        </p>
      )}
      {notice && (
        <p className="row-meta" data-testid="queue-notice">
          {notice}
        </p>
      )}
      <div className="actions">
        <label className="row-meta">
          Issue{' '}
          <input
            type="text"
            inputMode="numeric"
            placeholder="#"
            aria-label="Issue number"
            data-testid="queue-issue"
            value={issue}
            onChange={(e) => setIssue(e.target.value)}
          />
        </label>{' '}
        <button
          type="button"
          data-testid="queue-enqueue"
          onClick={() => void guarded(onEnqueue)}
        >
          Queue {repo} issue
        </button>
      </div>
      {groups.length === 0 ? (
        <div data-testid="queue-empty">
          <h3>No lane items</h3>
          <p className="row-meta">Queue an issue above to dispatch it from here.</p>
        </div>
      ) : (
        groups.map((group) => (
          <QueueRepoGroup
            key={group.repo}
            group={group}
            gatesByKey={gatesByKey}
            onDispatch={(item) => void guarded(() => onDispatch(item))}
            onDispatchNext={onDispatchNext}
            onCancel={(item) => void guarded(() => onCancel(item))}
            onGates={(item) => void guarded(() => onGates(item))}
          />
        ))
      )}
    </section>
  );
}
