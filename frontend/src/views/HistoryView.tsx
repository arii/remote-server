import { useCallback, useEffect, useState } from 'react';
import { fetchJobs, fetchLedger } from '../apiClient';
import type { JobRecord, LedgerEntry } from '../apiTypes';
import { loadHistory, useMock } from './mockViews';
import { toHistoryRows } from './historyUtils';
import type { HistoryRow } from './historyUtils';
import ObservabilitySection from './ObservabilitySection';

function useHistoryData() {
  const [jobs, setJobs] = useState<JobRecord[] | null>(null);
  const [entries, setEntries] = useState<LedgerEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      if (useMock()) {
        const h = loadHistory();
        setJobs(h.jobs);
        setEntries(h.entries);
      } else {
        const [j, l] = await Promise.all([fetchJobs(), fetchLedger('all', 100)]);
        setJobs(j.jobs);
        setEntries(l.entries);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return { jobs, entries, error, loading, load };
}

function kindOptions(entries: LedgerEntry[]): string[] {
  return ['all', ...Array.from(new Set(entries.map((e) => e.kind)))];
}

function filterByKind(entries: LedgerEntry[], kind: string): LedgerEntry[] {
  if (kind === 'all') return entries;
  return entries.filter((e) => e.kind === kind);
}

function RowsList({ rows }: { rows: HistoryRow[] }) {
  if (rows.length === 0) {
    return (
      <p className="row-meta" data-testid="history-empty">
        No history yet.
      </p>
    );
  }
  return (
    <ul className="session-list" data-testid="history-list">
      {rows.map((r) => (
        <li key={r.key} data-testid={r.repeat > 1 ? 'history-incident' : 'history-row'}>
          <span className="row-title">{r.summary}</span>{' '}
          <span className="row-meta">
            {r.kind} · {r.at}
          </span>{' '}
          <span className="row-meta">{r.recovery}</span>
        </li>
      ))}
    </ul>
  );
}

export default function HistoryView({ repo }: { repo: string }) {
  const { jobs, entries, error, loading, load } = useHistoryData();
  const [kind, setKind] = useState('all');
  const all = entries ?? [];
  const rows = toHistoryRows(jobs ?? [], filterByKind(all, kind));

  return (
    <div data-testid="history-view">
      <ObservabilitySection repo={repo} />

      <section aria-label="History">
        <div className="page-head">
          <div>
            <h2>History</h2>
            <p className="row-meta">Jobs and ledger events, newest first.</p>
          </div>
          <div className="actions" role="group" aria-label="History actions">
            <label className="row-meta" htmlFor="history-kind">
              Kind{' '}
              <select
                id="history-kind"
                data-testid="history-kind-filter"
                value={kind}
                onChange={(e) => setKind(e.target.value)}
              >
                {kindOptions(all).map((k) => (
                  <option key={k} value={k}>
                    {k}
                  </option>
                ))}
              </select>
            </label>
            <button type="button" data-testid="history-refresh" onClick={() => void load()}>
              Refresh
            </button>
          </div>
        </div>

        {error && (
          <div data-testid="history-error" role="alert">
            History unavailable: {error}
          </div>
        )}
        {loading && <div data-testid="history-loading">Loading history…</div>}
        {!loading && !error && <RowsList rows={rows} />}
      </section>
    </div>
  );
}
