import { useEffect, useState } from 'react';
import TapToFire from '../components/TapToFire';
import { isRollupRepo } from '../components/RepoPicker';
import {
  apiUrl,
  createIssue,
  fetchDeferred,
  fetchInbox,
  fetchRepoLabels,
  postActionHref,
  snoozeAttention,
  unsnoozeAttention,
  warmViewCaches,
} from '../apiClient';
import type { InboxFeed, SystemHealthResponse } from '../apiClient';
import type { AttentionItem } from '../attentionTypes';
import type { DeferredRow } from '../reviewTypes';
import { loadInbox, loadInboxEmpty } from '../mocks/inbox';
import { ContainerPills } from './ObservabilitySection';
import InboxStrips from './InboxStrips';
import ReviewView from './ReviewView';
import JobsView from './JobsView';
import { useMock } from './mockViews';

function isGetAction(item: AttentionItem): boolean {
  return item.action.method.toUpperCase() === 'GET';
}

async function firePostAction(item: AttentionItem): Promise<void> {
  // Central outbox: offline taps queue + throw visibly instead of
  // failing with no recourse (issue #404).
  await postActionHref(item.action.href, { intent: item.action.intent, confirmed: true });
}

// GET action navigation (issue #464): backend hrefs point at JSON
// endpoints whose payloads already have in-app surfaces. Navigate
// there instead of fetching-and-discarding. Unknown hrefs keep the
// fetch-summary fallback. Actions must never render as plain links:
// that navigates the SPA away to raw JSON.
export type AttentionNavTarget = 'jobs' | 'review' | 'history' | 'briefing' | 'chat';

export function resolveAttentionTarget(href: string): AttentionNavTarget | null {
  const path = href.split('?')[0].split('#')[0].replace(/^\/+/, '');
  if (path === 'api/jobs' || path.startsWith('api/jobs/')) return 'jobs';
  if (path === 'api/prs' || path.startsWith('api/prs/')) return 'review';
  if (path === 'api/workflow-health') return 'history';
  if (path === 'api/notifications/status') return 'briefing';
  if (path === 'api/agents/budget') return 'chat';
  return null;
}

export type AttentionNavTab = 'history' | 'briefing' | 'chat';

async function fireGetAction(item: AttentionItem): Promise<string> {
  const resp = await fetch(apiUrl(item.action.href), { method: 'GET' });
  if (!resp.ok) throw new Error(`Action failed: ${resp.status}`);
  let summary = '';
  try {
    const body: unknown = await resp.json();
    if (body && typeof body === 'object') {
      const o = body as Record<string, unknown>;
      const cand = o.message ?? o.detail ?? o.status ?? o.url;
      if (typeof cand === 'string' && cand) summary = cand;
    }
  } catch {
    summary = '';
  }
  return summary ? `${item.action.label}: ${summary}` : `${item.action.label}: done.`;
}

function formatStamp(epochSeconds: number): string {
  if (!epochSeconds) return 'unknown time';
  return new Date(epochSeconds * 1000).toLocaleString();
}

function relativeStamp(epochSeconds: number): string {
  if (!epochSeconds) return 'unknown time';
  const mins = Math.max(0, Math.round((Date.now() / 1000 - epochSeconds) / 60));
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

// Live queue cadence: re-fetch fast while items need the operator,
// back off to a slow heartbeat when the queue is clear.
export function nowPollMs(itemCount: number): number {
  return itemCount > 0 ? 15000 : 60000;
}

// Attention snooze presets (issue #614), minutes. Server validates.
export const SNOOZE_PRESETS = [60, 480];

export function snoozePresetLabel(minutes: number): string {
  return minutes >= 60 ? `Snooze ${minutes / 60}h` : `Snooze ${minutes}m`;
}

export interface SnoozedEntry {
  until: number;
  title: string;
}

// Drop stubs that no longer apply: expired ones (the item resurfaces
// on its own) and stubs whose item is back in the list (server truth
// wins over optimism).
export function reconcileSnoozed(
  prev: Record<string, SnoozedEntry>,
  liveIds: string[],
  nowSec: number,
): Record<string, SnoozedEntry> {
  const next: Record<string, SnoozedEntry> = {};
  for (const [id, entry] of Object.entries(prev)) {
    if (entry.until > nowSec && !liveIds.includes(id)) next[id] = entry;
  }
  return next;
}

// Stubs for snoozed ids missing from the live list (the undo row);
// present ids render their stub inline instead.
export function snoozedRows(
  snoozed: Record<string, SnoozedEntry>,
  liveIds: string[],
): Array<[string, SnoozedEntry]> {
  return Object.entries(snoozed).filter(([id]) => !liveIds.includes(id));
}

export function SnoozeButtons({
  itemId,
  busy,
  onSnooze,
}: {
  itemId: string;
  busy: boolean;
  onSnooze: (minutes: number) => void;
}) {
  return (
    <>
      {SNOOZE_PRESETS.map((m) => (
        <button
          key={m}
          type="button"
          data-testid={`snooze-${itemId}-${m}`}
          disabled={busy}
          onClick={() => onSnooze(m)}
        >
          {snoozePresetLabel(m)}
        </button>
      ))}
    </>
  );
}

export function SnoozedStub({
  itemId,
  title,
  until,
  busy,
  onUnsnooze,
}: {
  itemId: string;
  title: string;
  until: number;
  busy: boolean;
  onUnsnooze: () => void;
}) {
  return (
    <li className="detail" data-testid={`now-snoozed-${itemId}`}>
      <span className="row-title">Snoozed: {title}</span>
      <p className="row-meta">Hidden until {formatStamp(until)}</p>
      <div className="actions">
        <button
          type="button"
          data-testid={`unsnooze-${itemId}`}
          disabled={busy}
          onClick={onUnsnooze}
        >
          {busy ? 'Unsnoozing…' : 'Unsnooze'}
        </button>
      </div>
    </li>
  );
}

export function StackSection({ health }: { health: SystemHealthResponse | null }) {
  const allUp =
    health !== null &&
    health.containers.length > 0 &&
    health.containers.every(
      (c) =>
        (c.state ?? '').toLowerCase() === 'running' &&
        (c.health ?? '').toLowerCase() !== 'unhealthy',
    );
  return (
    <section data-testid="now-stack" aria-label="Stack">
      <h2>Stack</h2>
      {!health ? (
        <p className="row-meta" role="status">Stack status: loading…</p>
      ) : allUp ? (
        <p className="row-meta" data-testid="now-stack-clear" role="status">
          All {health.containers.length} containers up — all clear.
        </p>
      ) : (
        <ContainerPills containers={health.containers} error={health.containers_error} />
      )}
    </section>
  );
}

// Repo tag (issue #610, extended #723): rollup rows always tag;
// single-repo rows tag only when the row's repo differs from the
// view — foreign rows should not occur after the backend scoping
// fix, so single-repo mode stays pixel-identical and the tag is
// the backstop that keeps a leak distinguishable.
// Guided empty state (issue #809): what lands here plus the
// one-tap first action (opens the Jobs section to run a check).
export function NowEmptyState({ onRunCheck }: { onRunCheck: () => void }) {
  return (
    <div data-testid="now-empty">
      <h3>Nothing needs you right now</h3>
      <p className="row-meta">Failing checks, approvals, and incidents will land here.</p>
      <div className="actions">
        <button type="button" data-testid="now-empty-jobs" onClick={onRunCheck}>
          Run a check
        </button>
      </div>
    </div>
  );
}

export function RepoTag({ repo }: { repo: string }) {
  return (
    <span className="repo-chip" data-testid={`now-repo-${repo}`}>
      {repo}
    </span>
  );
}

export function showRepoTag(rollup: boolean, itemRepo: string, viewRepo: string): boolean {
  return rollup || itemRepo !== viewRepo;
}

// Ask-agent seed (issue #723): always the row's own repo, never
// the view repo — a foreign row must still seed its own context.
export function askAgentSeed(item: AttentionItem): string {
  return `${item.title} (${item.repo}): ${item.detail}`;
}

function sevClass(severity: string): string {
  const s = severity.toLowerCase();
  if (s === 'crit' || s === 'critical') return 'sev sev-crit';
  if (s === 'warn' || s === 'warning') return 'sev sev-warn';
  return 'sev sev-info';
}

export function ReportDialog({
  initialBody,
  availableLabels = [],
  onSubmit,
  onCancel,
}: {
  initialBody: string;
  availableLabels?: string[];
  onSubmit: (title: string, body: string, labels: string[]) => void;
  onCancel: () => void;
}) {
  const [title, setTitle] = useState('');
  const [body, setBody] = useState(initialBody);
  const [labels, setLabels] = useState<string[]>([]);
  function toggleLabel(name: string) {
    setLabels((prev) => (prev.includes(name) ? prev.filter((l) => l !== name) : [...prev, name]));
  }
  // Inline (non-modal) dialog: labelled, Escape-dismissible, focus lands in
  // the title field on open. No focus trap: the page behind stays usable.
  return (
    <form
      data-testid="report-dialog"
      role="dialog"
      aria-label="Report a bug"
      onKeyDown={(e) => {
        if (e.key === 'Escape') {
          e.preventDefault();
          onCancel();
        }
      }}
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit(title.trim(), body, labels);
      }}
    >
      {availableLabels.length > 0 && (
        <fieldset data-testid="report-labels">
          <legend className="row-meta">Labels (optional)</legend>
          {availableLabels.map((name) => (
            <label key={name} className="row-meta">
              <input
                type="checkbox"
                data-testid={`report-label-${name}`}
                checked={labels.includes(name)}
                onChange={() => toggleLabel(name)}
              />{' '}
              {name}
            </label>
          ))}
        </fieldset>
      )}
      <label className="row-meta" htmlFor="report-title">
        Bug title (required){' '}
        <input
          id="report-title"
          data-testid="report-title"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          required
          aria-required="true"
          autoFocus
        />
      </label>
      <label className="row-meta" htmlFor="report-body">
        Details (optional){' '}
        <textarea
          id="report-body"
          data-testid="report-body"
          value={body}
          onChange={(e) => setBody(e.target.value)}
        />
      </label>
      <div className="actions">
        <button type="submit" data-testid="report-submit">
          File bug
        </button>
        <button type="button" data-testid="report-cancel" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

export function ActionControl({
  item,
  onDone,
  onError,
  onSection,
  onTab,
}: {
  item: AttentionItem;
  onDone: (title: string) => void;
  onError: (message: string) => void;
  onSection: (section: 'review' | 'jobs') => void;
  onTab: (tab: AttentionNavTab) => void;
}) {
  async function handlePost() {
    try {
      await firePostAction(item);
      onDone(item.title);
    } catch (err) {
      onError(err instanceof Error ? err.message : String(err));
    }
  }
  async function handleGet() {
    const target = resolveAttentionTarget(item.action.href);
    if (target === 'jobs') {
      onSection('jobs');
      return;
    }
    if (target === 'review') {
      onSection('review');
      return;
    }
    if (target !== null) {
      onTab(target);
      return;
    }
    try {
      onDone(await fireGetAction(item));
    } catch (err) {
      onError(err instanceof Error ? err.message : String(err));
    }
  }
  if (isGetAction(item)) {
    return (
      <button type="button" onClick={() => void handleGet()}>
        {item.action.label}
      </button>
    );
  }
  if (item.action.destructive) {
    return (
      <TapToFire
        label={item.action.label}
        confirm="This cannot be undone."
        testid={`now-action-${item.id}`}
        onFire={() => void handlePost()}
      />
    );
  }
  return (
    <button type="button" onClick={() => void handlePost()}>
      {item.action.label}
    </button>
  );
}

export default function NowView({
  repo,
  defaultBranch,
  health,
  reportContext,
  onAskAgent,
  onOpenTab,
}: {
  repo: string;
  // Checkout branch of the viewed repo, seeding the Review/Jobs
  // branch fields (#708).
  defaultBranch?: string;
  health: SystemHealthResponse | null;
  reportContext: string;
  onAskAgent: (seed: string) => void;
  onOpenTab: (tab: AttentionNavTab) => void;
}) {
  const [data, setData] = useState<InboxFeed | null>(null);
  // Deferred flags backing the strips bulk bar (issue #591). The
  // strips render outside the rollup only; rollup keeps [].
  const [deferred, setDeferred] = useState<DeferredRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [fired, setFired] = useState<string | null>(null);
  const [reported, setReported] = useState<string | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  // Repo labels for the report dialog checkboxes (issue #436):
  // fetched once when the dialog first opens, like ReviewView's
  // typeahead suggestions; null until loaded.
  const [reportLabels, setReportLabels] = useState<string[] | null>(null);
  // One section mounted at a time: Review and Jobs each bind window
  // triage keys, so mounting both would double-bind j/k/Enter.
  const [section, setSection] = useState<'review' | 'jobs' | null>(null);
  // Last Review row selection, surviving section switches so Jobs can
  // prefill its PR field from row context. Reset on repo change.
  const [lastPr, setLastPr] = useState<number | null>(null);
  // Optimistic snooze stubs by item id. A stub collapses its card
  // instantly; refresh() reconciles against the server list.
  const [snoozed, setSnoozed] = useState<Record<string, SnoozedEntry>>({});
  const [snoozeBusy, setSnoozeBusy] = useState<string | null>(null);
  const rollup = isRollupRepo(repo);

  useEffect(() => {
    setLastPr(null);
    // Review/Jobs are per-repo sections; a repo switch (in or out
    // of the rollup) closes whichever is open.
    setSection(null);
  }, [repo]);

  async function refresh() {
    try {
      const live = await fetchInbox(repo);
      setData(live);
      if (!rollup) {
        try {
          const d = await fetchDeferred(repo);
          setDeferred(d.deferred);
        } catch {
          // Strips keep their last flags; Review shows its own error.
        }
      }
      setSnoozed((prev) =>
        reconcileSnoozed(
          prev,
          live.attention.map((i) => i.id),
          Date.now() / 1000,
        ),
      );
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  function dropStub(id: string) {
    setSnoozed((prev) => {
      if (!(id in prev)) return prev;
      const next = { ...prev };
      delete next[id];
      return next;
    });
  }

  async function onSnooze(item: AttentionItem, minutes: number) {
    if (snoozeBusy !== null) return;
    const optimisticUntil = Date.now() / 1000 + minutes * 60;
    setSnoozed((prev) => ({ ...prev, [item.id]: { until: optimisticUntil, title: item.title } }));
    setSnoozeBusy(item.id);
    try {
      // Snooze against the row's own repo: the view repo is the
      // rollup pseudo-repo in All repos mode, where rows carry
      // their own repos.
      const res = await snoozeAttention(item.id, minutes, item.repo);
      // Re-assert with the server timestamp: a poll refresh may have
      // dropped the optimistic stub mid-flight.
      setSnoozed((prev) => ({ ...prev, [item.id]: { until: res.snoozed_until, title: item.title } }));
      setFired(`Snoozed: ${item.title} until ${formatStamp(res.snoozed_until)}`);
      await refresh();
    } catch (err) {
      dropStub(item.id);
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSnoozeBusy(null);
    }
  }

  async function onUnsnooze(id: string) {
    if (snoozeBusy !== null) return;
    setSnoozeBusy(id);
    try {
      await unsnoozeAttention(id);
      dropStub(id);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSnoozeBusy(null);
    }
  }

  useEffect(() => {
    let cancelled = false;
    if (useMock()) {
      const params = new URLSearchParams(window.location.search);
      const empty = params.get('empty') === '1';
      setData(empty ? loadInboxEmpty() : loadInbox());
      setDeferred([]);
      return;
    }
    (async () => {
      try {
        const live = await fetchInbox(repo);
        if (!cancelled) setData(live);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
      if (!rollup) {
        try {
          const d = await fetchDeferred(repo);
          if (!cancelled) setDeferred(d.deferred);
        } catch {
          // Deferred flags load independently: strips stay empty.
          if (!cancelled) setDeferred([]);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repo]);

  // Warm slow view reads on idle so first Briefing/Jobs visits land
  // instantly instead of generating on click.
  useEffect(() => {
    if (useMock() || rollup) return;
    const warm = () => void warmViewCaches(repo);
    if (typeof window.requestIdleCallback === 'function') {
      const id = window.requestIdleCallback(warm);
      return () => window.cancelIdleCallback(id);
    }
    const t = window.setTimeout(warm, 0);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repo]);

  // Live queue: re-fetch on the adaptive cadence so new items land
  // without a reload. Paused in a hidden tab and while reporting.
  useEffect(() => {
    if (useMock() || !data || dialogOpen) return;
    const timer = window.setInterval(
      () => {
        if (!document.hidden) void refresh();
      },
      nowPollMs(data.attention.length),
    );
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repo, data, dialogOpen]);

  async function ensureReportLabels() {
    if (reportLabels !== null || useMock()) return;
    try {
      const r = await fetchRepoLabels(rollup ? 'remote-server' : repo);
      setReportLabels(r.labels);
    } catch {
      setReportLabels([]);
    }
  }

  async function onSubmitReport(title: string, body: string, labels: string[]) {
    setError(null);
    try {
      // Console bugs file against the console's own repo; the
      // rollup pseudo-repo is not a real checkout.
      const result = await createIssue(rollup ? 'remote-server' : repo, title, body, labels);
      setReported(result.url || 'created');
      setDialogOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  if (!data && !error) return <section data-testid="now-loading" role="status">Loading…</section>;

  async function onRetry() {
    setError(null);
    await refresh();
  }

  const stubRows = data ? snoozedRows(snoozed, data.attention.map((i) => i.id)) : [];

  return (
    <section data-testid="now-view">
      <header className="page-head">
        <div>
          <h2>
            Now{' '}
            {data && (
              <span className="head-stamp" title={formatStamp(data.updated_at)}>
                Updated {relativeStamp(data.updated_at)}
              </span>
            )}
          </h2>
        </div>
        <button type="button" data-testid="report-bug" onClick={() => { setDialogOpen(true); void ensureReportLabels(); }}>
          Report a bug
        </button>
      </header>
      {error && (
        <div data-testid="now-error" role="alert">
          Couldn&apos;t load what needs you: {error}{' '}
          <button type="button" data-testid="now-retry" onClick={() => void onRetry()}>
            Retry
          </button>
        </div>
      )}
      {dialogOpen && (
        <ReportDialog
          initialBody={reportContext}
          availableLabels={reportLabels ?? []}
          onSubmit={(t, b, l) => void onSubmitReport(t, b, l)}
          onCancel={() => setDialogOpen(false)}
        />
      )}
      {reported && (
        <p className="row-meta" data-testid="report-done" role="status">
          Thanks — filed{reported !== 'created' ? `: ${reported}` : '.'}
        </p>
      )}
      {fired && (
        <p className="row-meta" data-testid="now-fired" role="status">
          Done: {fired}
        </p>
      )}
      <StackSection health={health} />
      {data && (
        <>
          {!rollup && (
            <InboxStrips
              feed={data}
              repo={repo}
              deferred={deferred}
              onDeferredChange={setDeferred}
              onOpenReview={() => setSection('review')}
              onOpenJobs={() => setSection('jobs')}
            />
          )}
          {stubRows.length > 0 && (
            <ul className="session-list" data-testid="now-snoozed">
              {stubRows.map(([id, entry]) => (
                <SnoozedStub
                  key={id}
                  itemId={id}
                  title={entry.title}
                  until={entry.until}
                  busy={snoozeBusy === id}
                  onUnsnooze={() => void onUnsnooze(id)}
                />
              ))}
            </ul>
          )}
      {data.attention.length === 0 ? (
        <NowEmptyState onRunCheck={() => setSection('jobs')} />
      ) : (
        <ul className="session-list" data-testid="now-list">
          {data.attention.map((item) =>
            snoozed[item.id] ? (
              <SnoozedStub
                key={item.id}
                itemId={item.id}
                title={item.title}
                until={snoozed[item.id].until}
                busy={snoozeBusy === item.id}
                onUnsnooze={() => void onUnsnooze(item.id)}
              />
            ) : (
            <li key={item.id} className="detail" data-testid={`now-item-${item.id}`}>
              {showRepoTag(rollup, item.repo, repo) ? (
                <span className="row-title">
                  <RepoTag repo={item.repo} /> {item.title}
                </span>
              ) : (
                <span className="row-title">{item.title}</span>
              )}
              <p className="row-meta">
                {item.kind} · <span className={sevClass(item.severity)}>{item.severity}</span>
                {' · '}
                <span title={formatStamp(item.created_at)}>
                  {relativeStamp(item.created_at)}
                </span>
              </p>
              <p>{item.detail}</p>
              <div className="actions">
                <ActionControl
                  item={item}
                  onDone={(title) => {
                    setFired(title);
                    void refresh();
                  }}
                  onError={setError}
                  onSection={(s) => setSection(s)}
                  onTab={(t) => onOpenTab(t)}
                />
                <button
                  type="button"
                  data-testid={`ask-agent-${item.id}`}
                  onClick={() => onAskAgent(askAgentSeed(item))}
                >
                  Ask agent
                </button>
                <SnoozeButtons
                  itemId={item.id}
                  busy={snoozeBusy !== null}
                  onSnooze={(m) => void onSnooze(item, m)}
                />
              </div>
            </li>
            ),
          )}
        </ul>
      )}
      {!rollup && (
        <div className="actions" data-testid="inbox-sections">
          <button
            type="button"
            data-testid="inbox-open-review"
            aria-pressed={section === 'review'}
            onClick={() => setSection((s) => (s === 'review' ? null : 'review'))}
          >
            Review
          </button>
          <button
            type="button"
            data-testid="inbox-open-jobs"
            aria-pressed={section === 'jobs'}
            onClick={() => setSection((s) => (s === 'jobs' ? null : 'jobs'))}
          >
            Jobs{data.runs.active ? ' •' : ''}
          </button>
        </div>
      )}
          {section === 'review' && <ReviewView repo={repo} defaultBranch={defaultBranch} onAskAgent={onAskAgent} onSelectPr={setLastPr} />}
          {section === 'jobs' && <JobsView repo={repo} defaultBranch={defaultBranch} onAskAgent={onAskAgent} initialPr={lastPr} />}
        </>
      )}
    </section>
  );
}
