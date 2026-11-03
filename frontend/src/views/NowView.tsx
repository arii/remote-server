import { useEffect, useState } from 'react';
import HoldToConfirm from '../components/HoldToConfirm';
import { apiUrl, createIssue, fetchInbox } from '../apiClient';
import type { InboxFeed, SystemHealthResponse } from '../apiClient';
import type { AttentionItem } from '../attentionTypes';
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
  const resp = await fetch(apiUrl(item.action.href), {
    method: item.action.method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ intent: item.action.intent, confirmed: true }),
  });
  if (!resp.ok) throw new Error(`Action failed: ${resp.status}`);
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

function sevClass(severity: string): string {
  const s = severity.toLowerCase();
  if (s === 'crit' || s === 'critical') return 'sev sev-crit';
  if (s === 'warn' || s === 'warning') return 'sev sev-warn';
  return 'sev sev-info';
}

function ReportDialog({
  initialBody,
  onSubmit,
  onCancel,
}: {
  initialBody: string;
  onSubmit: (title: string, body: string) => void;
  onCancel: () => void;
}) {
  const [title, setTitle] = useState('');
  const [body, setBody] = useState(initialBody);
  return (
    <form
      data-testid="report-dialog"
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit(title.trim(), body);
      }}
    >
      <label className="row-meta" htmlFor="report-title">
        Bug title (required){' '}
        <input
          id="report-title"
          data-testid="report-title"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          required
          aria-required="true"
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

function ActionControl({
  item,
  onDone,
  onError,
}: {
  item: AttentionItem;
  onDone: (title: string) => void;
  onError: (message: string) => void;
}) {
  async function handlePost() {
    try {
      await firePostAction(item);
      onDone(item.title);
    } catch (err) {
      onError(err instanceof Error ? err.message : String(err));
    }
  }
  if (isGetAction(item)) {
    return <a href={apiUrl(item.action.href)}>{item.action.label}</a>;
  }
  if (item.action.destructive) {
    return <HoldToConfirm label={item.action.label} onFire={() => void handlePost()} />;
  }
  return (
    <button type="button" onClick={() => void handlePost()}>
      {item.action.label}
    </button>
  );
}

export default function NowView({
  repo,
  health,
  reportContext,
  onAskAgent,
}: {
  repo: string;
  health: SystemHealthResponse | null;
  reportContext: string;
  onAskAgent: (seed: string) => void;
}) {
  const [data, setData] = useState<InboxFeed | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fired, setFired] = useState<string | null>(null);
  const [reported, setReported] = useState<string | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  // One section mounted at a time: Review and Jobs each bind window
  // triage keys, so mounting both would double-bind j/k/Enter.
  const [section, setSection] = useState<'review' | 'jobs' | null>(null);

  async function refresh() {
    try {
      const live = await fetchInbox(repo);
      setData(live);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  useEffect(() => {
    let cancelled = false;
    if (useMock()) {
      const params = new URLSearchParams(window.location.search);
      const empty = params.get('empty') === '1';
      setData(empty ? loadInboxEmpty() : loadInbox());
      return;
    }
    (async () => {
      try {
        const live = await fetchInbox(repo);
        if (!cancelled) setData(live);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
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

  async function onSubmitReport(title: string, body: string) {
    setError(null);
    try {
      const result = await createIssue(repo, title, body);
      setReported(result.url || 'created');
      setDialogOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  if (!data && !error) return <section data-testid="now-loading">Loading…</section>;

  async function onRetry() {
    setError(null);
    await refresh();
  }

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
        <button type="button" data-testid="report-bug" onClick={() => setDialogOpen(true)}>
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
          onSubmit={(t, b) => void onSubmitReport(t, b)}
          onCancel={() => setDialogOpen(false)}
        />
      )}
      {reported && (
        <p className="row-meta" data-testid="report-done">
          Thanks — filed{reported !== 'created' ? `: ${reported}` : '.'}
        </p>
      )}
      {fired && (
        <p className="row-meta" data-testid="now-fired">
          Done: {fired}
        </p>
      )}
      <section data-testid="now-stack" aria-label="Stack">
        <h2>Stack</h2>
        {health ? (
          <ContainerPills containers={health.containers} />
        ) : (
          <p className="row-meta">Stack status: loading…</p>
        )}
      </section>
      {data && (
        <>
          <InboxStrips
            feed={data}
            onOpenReview={() => setSection('review')}
            onOpenJobs={() => setSection('jobs')}
          />
      {data.attention.length === 0 ? (
        <div data-testid="now-empty">
          <h3>Nothing needs you right now</h3>
          <p className="row-meta">All clear. Check back later.</p>
        </div>
      ) : (
        <ul className="session-list" data-testid="now-list">
          {data.attention.map((item) => (
            <li key={item.id} className="detail" data-testid={`now-item-${item.id}`}>
              <span className="row-title">{item.title}</span>
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
                />
                <button
                  type="button"
                  data-testid={`ask-agent-${item.id}`}
                  onClick={() =>
                    onAskAgent(`${item.title} (${item.repo}): ${item.detail}`)
                  }
                >
                  Ask agent
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
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
          {section === 'review' && <ReviewView repo={repo} onAskAgent={onAskAgent} />}
          {section === 'jobs' && <JobsView repo={repo} />}
        </>
      )}
    </section>
  );
}
