import { useEffect, useState } from 'react';
import HoldToConfirm from '../components/HoldToConfirm';
import type { AttentionItem, AttentionResponse } from '../attentionTypes';
import { loadAttention, loadAttentionEmpty, shouldUseMock } from '../mocks/attention';

async function fetchAttention(repo: string): Promise<AttentionResponse> {
  const resp = await fetch(`/api/attention?repo=${encodeURIComponent(repo)}`);
  if (!resp.ok) throw new Error(`GET /api/attention failed: ${resp.status}`);
  return (await resp.json()) as AttentionResponse;
}

async function fireAction(item: AttentionItem): Promise<void> {
  const { action } = item;
  const method = action.method.toUpperCase();
  if (method === 'GET') {
    window.location.href = action.href;
    return;
  }
  const resp = await fetch(action.href, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ intent: action.intent, confirmed: true }),
  });
  if (!resp.ok) throw new Error(`Action failed: ${resp.status}`);
}

async function reportBug(repo: string): Promise<string> {
  const title = window.prompt('Bug title', '')?.trim() ?? '';
  if (!title) throw new Error('cancelled');
  const body = window.prompt('Bug details (optional)', '') ?? '';
  const resp = await fetch('/api/create-issue', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ repo, title, body }),
  });
  if (!resp.ok) throw new Error(`Report failed: ${resp.status}`);
  const data = (await resp.json()) as { url?: string };
  return data.url ?? '';
}

export default function NowView({ repo }: { repo: string }) {
  const [data, setData] = useState<AttentionResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fired, setFired] = useState<string | null>(null);
  const [reported, setReported] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        if (shouldUseMock()) {
          const params = new URLSearchParams(window.location.search);
          const empty = params.get('empty') === '1';
          if (!cancelled) setData(empty ? loadAttentionEmpty() : loadAttention());
          return;
        }
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

  async function onReport() {
    setError(null);
    try {
      const url = await reportBug(repo);
      setReported(url || 'created');
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      if (msg !== 'cancelled') setError(msg);
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
          <p className="row-meta">Updated {data.updated_at}</p>
        </div>
        <button type="button" data-testid="report-bug" onClick={onReport}>
          Report a bug
        </button>
      </header>
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
                {item.kind} · {item.severity} · {item.created_at}
              </p>
              <p>{item.detail}</p>
              {item.action.destructive ? (
                <HoldToConfirm
                  label={item.action.label}
                  onFire={() => {
                    fireAction(item)
                      .then(() => setFired(item.title))
                      .catch((e: unknown) =>
                        setError(e instanceof Error ? e.message : String(e)),
                      );
                  }}
                />
              ) : (
                <button
                  type="button"
                  onClick={() => {
                    fireAction(item)
                      .then(() => setFired(item.title))
                      .catch((e: unknown) =>
                        setError(e instanceof Error ? e.message : String(e)),
                      );
                  }}
                >
                  {item.action.label}
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
