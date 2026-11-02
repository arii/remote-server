import { useEffect, useState } from 'react';
import { fetchLedger } from '../apiClient';
import type { LedgerResponse } from '../apiTypes';
import { loadLedger, useMock } from './mockViews';

export default function LedgerView() {
  const [data, setData] = useState<LedgerResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const live = useMock() ? loadLedger() : await fetchLedger();
        if (!cancelled) setData(live);
      } catch (err) {
        if (!cancelled)
          setError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (error)
    return (
      <div data-testid="ledger-error" role="alert">
        Ledger unavailable: {error}
      </div>
    );
  if (!data) return <div data-testid="ledger-loading">Loading ledger…</div>;
  return (
    <section data-testid="ledger-view">
      <ul className="session-list">
        {data.entries.map((e, i) => (
          <li key={`${e.kind}-${i}`}>
            <span className="row-title">{e.event ?? e.kind}</span>{' '}
            <span className="row-meta">
              {e.kind}
              {e.repo ? ` · ${e.repo}` : ''}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
