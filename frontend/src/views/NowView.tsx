import { useEffect, useState } from 'react';
import HoldToConfirm from '../components/HoldToConfirm';
import { createIssue, fetchAttention } from '../apiClient';
import type { AttentionItem, AttentionResponse } from '../attentionTypes';
import { loadAttention, loadAttentionEmpty } from '../mocks/attention';
import { useMock } from './mockViews';

function isGetAction(item: AttentionItem): boolean {
  return item.action.method.toUpperCase() === 'GET';
}

async function firePostAction(item: AttentionItem): Promise<void> {
  const resp = await fetch(item.action.href, {
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

function ReportDialog({
  onSubmit,
  onCancel,
}: {
  onSubmit: (title: string, body: string) => void;
  onCancel: () => void;
}) {
  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');
  return (
    <form
      data-testid="report-dialog"
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit(title.trim(), body);
      }}
    >
      <label className="row-meta" htmlFor="report-title">
        Bug title{' '}
        <input
          id="report-title"
          data-testid="report-title"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          required
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
    return <a href={item.action.href}>{item.action.label}</a>;
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

export default function NowView({ repo }: { repo: string }) {
  const [data, setData] = useState<AttentionResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fired, setFired] = useState<string | null>(null);
  const [reported, setReported] = useState<string | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);

  async function refresh() {
    try {
      const live = await fetchAttention(repo);
      setData(live);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  useEffect(() => {
    let cancelled = false;
    if (useMock()) {
      const params = new URLSearchParams(window.location.search);
      const empty = params.get('empty') === '1';
      setData(empty ? loadAttentionEmpty() : loadAttention());
      return;
    }
    (async () => {
      try {
        const live = await fetchAttention(repo);
        if (!cancelled) setData(live);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [repo]);

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

  if (error) {
    return (
      <section data-testid="now-error" role="alert">
        Couldn&apos;t load what needs you: {error}
      </section>
    );
  }
  if (!data) return <section data-testid="now-loading">Loading…</section>;

  return (
    <section data-testid="now-view">
      <header className="page-head">
        <div>
          <h2>Now</h2>
          <p className="row-meta">Updated {formatStamp(data.updated_at)}</p>
        </div>
        <button type="button" data-testid="report-bug" onClick={() => setDialogOpen(true)}>
          Report a bug
        </button>
      </header>
      {dialogOpen && (
        <ReportDialog
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
      {data.items.length === 0 ? (
        <div data-testid="now-empty">
          <h3>Nothing needs you right now</h3>
          <p className="row-meta">All clear. Check back later.</p>
        </div>
      ) : (
        <ul className="session-list" data-testid="now-list">
          {data.items.map((item) => (
            <li key={item.id} className="detail" data-testid={`now-item-${item.id}`}>
              <span className="row-title">{item.title}</span>
              <p className="row-meta">
                {item.kind} · {item.severity} · {formatStamp(item.created_at)}
              </p>
              <p>{item.detail}</p>
              <ActionControl
                item={item}
                onDone={(title) => {
                  setFired(title);
                  void refresh();
                }}
                onError={setError}
              />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
