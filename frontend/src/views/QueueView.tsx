import { useCallback, useEffect, useState } from 'react';
import {
  cancelDispatch,
  enqueueDispatch,
  fetchAllIssues,
  fetchDispatchGates,
  fetchDispatchQueue,
  startDispatch,
} from '../apiClient';
import type { DispatchItem } from '../apiTypes';
import type { IssueRow } from '../reviewTypes';
import { isTabVisible, onTabVisible } from './jobsUtils';
import type { DispatchRepoGroup } from './queueUtils';
import {
  allQueuedAcrossGroups,
  groupQueueByRepo,
  nextDispatchable,
  queueItemKey,
  queueListActive,
  queueRowActions,
  queueRowMeta,
  splitBlockReasons,
} from './queueUtils';
import { loadDispatchQueue, useMock } from './mockViews';

export function QueueHeader({
  count,
  live,
  dispatchable,
  onRefresh,
  onDispatchAll,
}: {
  count: number;
  live: boolean;
  dispatchable: DispatchItem[];
  onRefresh: () => void;
  onDispatchAll: () => void;
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
      <button
        type="button"
        data-testid="queue-dispatch-all"
        disabled={dispatchable.length === 0}
        title={
          dispatchable.length === 0
            ? 'Nothing queued'
            : 'Dispatch every queued item; blocked ones report per item'
        }
        onClick={onDispatchAll}
      >
        Dispatch all ({dispatchable.length})
      </button>{' '}
      <button type="button" data-testid="queue-refresh" onClick={onRefresh}>
        Refresh
      </button>
    </header>
  );
}

export function QueueRow({
  item,
  gates,
  ready,
  onDispatch,
  onCancel,
  onGates,
}: {
  item: DispatchItem;
  gates: string[] | null;
  ready: boolean | null;
  onDispatch: (item: DispatchItem) => void;
  onCancel: (item: DispatchItem) => void;
  onGates: (item: DispatchItem) => void;
}) {
  const actions = queueRowActions(item.state);
  const terminal = item.state === 'done' || item.state === 'failed';
  const label = (
    <>
      {item.repo}#{item.issue}
      {item.title ? ` — ${item.title}` : ''}
    </>
  );
  return (
    <li
      key={`${item.repo}#${item.issue}`}
      className="detail"
      data-testid={`queue-${item.repo}-${item.issue}`}
    >
      {terminal && item.artifact_url ? (
        <a
          className="row-title"
          href={item.artifact_url}
          target="_blank"
          rel="noreferrer"
          data-testid={`queue-artifact-${item.repo}-${item.issue}`}
        >
          {label}
        </a>
      ) : (
        <span className="row-title">{label}</span>
      )}{' '}
      {ready === true && (
        <span className="row-meta" data-testid={`queue-ready-${item.repo}-${item.issue}`}>
          Ready
        </span>
      )}
      {ready === false && (
        <span className="row-meta" data-testid={`queue-blocked-${item.repo}-${item.issue}`}>
          Blocked
        </span>
      )}
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
  readyByKey,
  onDispatch,
  onDispatchNext,
  onCancel,
  onGates,
}: {
  group: DispatchRepoGroup;
  gatesByKey: Record<string, string[] | null>;
  readyByKey: Record<string, boolean>;
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
            gates={gatesByKey[queueItemKey(item.repo, item.issue)] ?? null}
            ready={readyByKey[queueItemKey(item.repo, item.issue)] ?? null}
            onDispatch={onDispatch}
            onCancel={onCancel}
            onGates={onGates}
          />
        ))}
      </ul>
    </section>
  );
}

// Cross-repo open-issue discovery (issue #882): one tagged list
// across every checkout; rows already in the queue read Queued
// instead of offering a second enqueue tap.
export function QueueDiscovery({
  issues,
  queuedKeys,
  onEnqueue,
}: {
  issues: IssueRow[];
  queuedKeys: Set<string>;
  onEnqueue: (row: IssueRow) => void;
}) {
  return (
    <section aria-label="Open issues">
      <h3>Open issues</h3>
      {issues.length === 0 ? (
        <p className="row-meta" data-testid="queue-discovery-empty">
          No open issues across checkouts.
        </p>
      ) : (
        <ul className="session-list" data-testid="queue-discovery-list">
          {issues.map((row) => {
            const key = queueItemKey(row.repo ?? '', row.number);
            const queued = queuedKeys.has(key);
            return (
              <li key={key} className="detail">
                <span
                  className="repo-chip"
                  data-testid={`queue-discovery-repo-${row.repo}-${row.number}`}
                >
                  {row.repo}
                </span>{' '}
                <span className="row-title">
                  #{row.number} {row.title}
                </span>
                <div className="actions">
                  {queued ? (
                    <span
                      className="row-meta"
                      data-testid={`queue-discovery-queued-${row.repo}-${row.number}`}
                    >
                      Queued
                    </span>
                  ) : (
                    <button
                      type="button"
                      data-testid={`queue-discovery-queue-${row.repo}-${row.number}`}
                      onClick={() => onEnqueue(row)}
                    >
                      Queue
                    </button>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
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
  const [readyByKey, setReadyByKey] = useState<Record<string, boolean>>({});
  const [openIssues, setOpenIssues] = useState<IssueRow[] | null>(null);
  const [discoveryError, setDiscoveryError] = useState<string | null>(null);

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

  const loadDiscovery = useCallback(async () => {
    if (useMock()) {
      setOpenIssues([]);
      return;
    }
    try {
      setOpenIssues((await fetchAllIssues()).issues);
    } catch (err) {
      setDiscoveryError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    void guarded(load);
    void loadDiscovery();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load, loadDiscovery]);

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

  // Gates auto-evaluate once per queued row (issue #882): rows
  // badge ready/blocked with zero taps, and the manual Check-gates
  // tap stays as re-check. Fetched keys never refetch on poll, so
  // the 5s list poll makes no gate traffic of its own.
  useEffect(() => {
    if (useMock()) return;
    if (!items) return;
    const missing = items.filter(
      (i) => i.state === 'queued' && gatesByKey[queueItemKey(i.repo, i.issue)] === undefined,
    );
    if (missing.length === 0) return;
    let cancelled = false;
    void (async () => {
      for (const item of missing) {
        try {
          const g = await fetchDispatchGates(item.repo, item.issue);
          if (cancelled) return;
          const key = queueItemKey(item.repo, item.issue);
          setGatesByKey((prev) => ({
            ...prev,
            [key]: g.ready ? ['Ready to dispatch.'] : g.reasons,
          }));
          setReadyByKey((prev) => ({ ...prev, [key]: g.ready }));
        } catch {
          // Leave the row unbadged; manual re-check recovers.
        }
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items]);

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

  async function onEnqueueKnown(row: IssueRow) {
    const queued = await enqueueDispatch(row.repo ?? repo, String(row.number), row.title);
    setNotice(`Queued ${queued.repo}#${queued.issue} — ${queued.title || row.title}.`);
    await load();
  }

  // One dispatch attempt as a result line, shared by the per-row
  // tap and Dispatch-all. 409 carries the joined gate reasons:
  // surface each line instead of the raw status line (Jobs
  // stale-row notices).
  async function attemptDispatch(item: DispatchItem): Promise<string> {
    try {
      await startDispatch(item.repo, item.issue);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      const detail = message.split(' 409 ').slice(1).join(' 409 ').trim();
      const reasons = detail ? splitBlockReasons(detail) : [];
      return reasons.length > 0
        ? `Blocked ${queueItemKey(item.repo, item.issue)}: ${reasons.join(' · ')}`
        : `Couldn't dispatch ${queueItemKey(item.repo, item.issue)}: ${message}`;
    }
    return `Dispatched ${queueItemKey(item.repo, item.issue)}.`;
  }

  async function onDispatch(item: DispatchItem) {
    setNotice(await attemptDispatch(item));
    await load();
  }

  async function onDispatchAll(targets: DispatchItem[]) {
    const lines: string[] = [];
    for (const item of targets) {
      lines.push(await attemptDispatch(item));
    }
    setNotice(lines.join(' '));
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
    const key = queueItemKey(item.repo, item.issue);
    const g = await fetchDispatchGates(item.repo, item.issue);
    setGatesByKey((prev) => ({
      ...prev,
      [key]: g.ready ? ['Ready to dispatch.'] : g.reasons,
    }));
    setReadyByKey((prev) => ({ ...prev, [key]: g.ready }));
  }

  if (!items && !error)
    return (
      <section data-testid="queue-loading" role="status">
        Loading dispatch queue…
      </section>
    );

  const list = items ?? [];
  const groups = groupQueueByRepo(list);
  const dispatchable = allQueuedAcrossGroups(groups);
  const queuedKeys = new Set(list.map((i) => queueItemKey(i.repo, i.issue)));

  return (
    <section data-testid="queue-view">
      <QueueHeader
        count={list.length}
        live={queueListActive(list)}
        dispatchable={dispatchable}
        onRefresh={() =>
          void guarded(async () => {
            await load();
            await loadDiscovery();
          })
        }
        onDispatchAll={() => void guarded(() => onDispatchAll(dispatchable))}
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
      {discoveryError && (
        <p role="alert" data-testid="queue-discovery-error">
          Couldn&apos;t load open issues: {discoveryError}
        </p>
      )}
      {openIssues === null && !discoveryError ? (
        <p className="row-meta" data-testid="queue-discovery-loading">
          Loading open issues…
        </p>
      ) : (
        <QueueDiscovery
          issues={openIssues ?? []}
          queuedKeys={queuedKeys}
          onEnqueue={(row) => void guarded(() => onEnqueueKnown(row))}
        />
      )}
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
            readyByKey={readyByKey}
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
