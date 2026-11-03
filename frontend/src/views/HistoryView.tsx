import { useCallback, useEffect, useState } from 'react';
import { fetchJobs, fetchLedger } from '../apiClient';
import type { JobRecord, LedgerEntry } from '../apiTypes';
import { loadHistory, useMock } from './mockViews';
import { jobPassesKindFilter, toHistoryRows } from './historyUtils';
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

function StatusIcon({ tone }: { tone: HistoryRow['tone'] }) {
  return (
    <svg
      className={`history-icon tone-${tone}`}
      viewBox="0 0 24 24"
      width="16"
      height="16"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="9" />
      {tone === 'ok' && <path d="M8.5 12.5l2.5 2.5 4.5-5" />}
      {tone === 'bad' && <path d="M9 9l6 6M15 9l-6 6" />}
      {tone === 'muted' && <path d="M8.5 12h7" />}
    </svg>
  );
}

function RowsList({ rows }: { rows: HistoryRow[] }) {
  if (rows.length === 0) {
    return (
      <p className="row-meta" data-testid="history-empty">
        No history yet.
      </p>
    );
  }
  // Identical recovery advice repeats across incidents; show each once.
  const shown = new Set<string>();
  return (
    <ul className="session-list" data-testid="history-list">
      {rows.map((r) => {
        const advice = r.recovery && !shown.has(r.recovery) ? r.recovery : '';
        if (advice) shown.add(advice);
        return (
          <li
            key={r.key}
            className="history-row"
            data-testid={r.repeat > 1 ? 'history-incident' : 'history-row'}
          >
            <details data-testid="history-detail">
              <summary>
                <span className="row-title">
                  <StatusIcon tone={r.tone} /> {r.summary}
                </span>
                <span className="row-meta">
                  {r.kind} · {r.at}
                  {advice && <> · Recovery: {advice}</>}
                </span>
              </summary>
              <pre className="row-meta">{r.detail}</pre>
            </details>
          </li>
        );
      })}
    </ul>
  );
}

export default function HistoryView({
  repo,
  health,
}: {
  repo: string;
  health: import('../apiClient').SystemHealthResponse | null;
}) {
  const { jobs, entries, error, loading, load } = useHistoryData();
  const [kind, setKind] = useState('all');
  const all = entries ?? [];
  const rows = toHistoryRows(
    (jobs ?? []).filter((j) => jobPassesKindFilter(j.kind, kind)),
    filterByKind(all, kind),
  );

  return (
    <div data-testid="history-view">
      <ObservabilitySection repo={repo} health={health} />

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
