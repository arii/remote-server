import { useState } from 'react';
import { fetchJobRun, fetchJobs, fetchLedger } from '../apiClient';
import type { JobRecord, LedgerEntry } from '../apiTypes';
import { loadHistory, useMock } from './mockViews';
import { historyRowMatches, jobPassesKindFilter, kindBadge, tailText, toHistoryRows } from './historyUtils';
import type { HistoryMember, HistoryRow } from './historyUtils';
import ObservabilitySection from './ObservabilitySection';
import ActivityTimeline from './ActivityTimeline';
import { useSnapshotData } from './useSnapshotData';
import { StatusIcon } from './StatusIcon';

interface HistorySnapshot {
  jobs: JobRecord[];
  entries: LedgerEntry[];
}

const EMPTY_HISTORY: HistorySnapshot = { jobs: [], entries: [] };

async function loadHistorySnapshot(): Promise<HistorySnapshot> {
  if (useMock()) {
    const h = loadHistory();
    return { jobs: h.jobs, entries: h.entries };
  }
  const [j, l] = await Promise.all([fetchJobs(), fetchLedger('all', 100)]);
  return { jobs: j.jobs, entries: l.entries };
}

function kindOptions(entries: LedgerEntry[]): string[] {
  return ['all', ...Array.from(new Set(entries.map((e) => e.kind)))];
}

function filterByKind(entries: LedgerEntry[], kind: string): LedgerEntry[] {
  if (kind === 'all') return entries;
  return entries.filter((e) => e.kind === kind);
}

// Lazy run-output disclosure, same onToggle pattern as the review diff:
// fetch only when opened. The runs registry is in-memory, so output for
// old runs 404s — surfaced as a plain note, not a crash.
function RunOutput({ runId }: { runId: string }) {
  const [output, setOutput] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const mock = useMock();

  async function load() {
    if (output !== null || error || mock) return;
    try {
      const p = await fetchJobRun(runId);
      setOutput(p.output || '');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <details
      className="history-run-output"
      data-testid={`history-run-output-${runId}`}
      onToggle={(e) => {
        if ((e.target as HTMLDetailsElement).open) void load();
      }}
    >
      <summary className="row-meta">Run output</summary>
      {mock && <p className="row-meta">Run output is unavailable in preview mode.</p>}
      {!mock && error && (
        <p className="row-meta" role="alert">
          Couldn&apos;t load run output: {error}
        </p>
      )}
      {!mock && !error && output !== null && (
        <pre className="row-meta" data-testid={`history-run-output-body-${runId}`}>
          {tailText(output) || 'No output recorded.'}
        </pre>
      )}
      {!mock && !error && output === null && <p className="row-meta">Loading…</p>}
    </details>
  );
}

function MemberBlock({ member, grouped }: { member: HistoryMember; grouped: boolean }) {
  return (
    <>
      <pre className="row-meta" data-testid="history-group-entry">
        {grouped ? `${member.at}\n${member.detail}` : member.detail}
      </pre>
      {member.runId && <RunOutput runId={member.runId} />}
    </>
  );
}

export function RowsList({
  rows,
  filtering,
  onRefresh = () => {},
}: {
  rows: HistoryRow[];
  filtering: boolean;
  onRefresh?: () => void;
}) {
  if (rows.length === 0) {
    if (filtering) {
      return (
        <p className="row-meta" data-testid="history-empty">
          No matching rows.
        </p>
      );
    }
    // Guided first-run state (issue #809): explainer plus a
    // working first action (re-fetch; runs land here once
    // dispatched from Now → Jobs).
    return (
      <div data-testid="history-empty">
        <h3>No history yet</h3>
        <p className="row-meta">
          Finished jobs and ledger events land here, newest first. Dispatch a run from Now →
          Jobs to see it here.
        </p>
        <div className="actions">
          <button type="button" data-testid="history-empty-refresh" onClick={onRefresh}>
            Refresh
          </button>
        </div>
      </div>
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
                  {kindBadge(r.kind)} · {r.at}
                  {advice && <> · Recovery: {advice}</>}
                </span>
              </summary>
              {r.members.map((m, mi) => (
                <MemberBlock key={mi} member={m} grouped={r.members.length > 1} />
              ))}
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
  const { data, error, loading, load } = useSnapshotData(loadHistorySnapshot, EMPTY_HISTORY);
  const { jobs, entries } = data;
  const [kind, setKind] = useState('all');
  const [query, setQuery] = useState('');
  const all = entries;
  const filtering = query.trim().length > 0;
  const rows = toHistoryRows(
    jobs.filter((j) => jobPassesKindFilter(j.kind, kind)),
    filterByKind(all, kind),
  ).filter((r) => historyRowMatches(r, query));

  return (
    <div data-testid="history-view">
      <ObservabilitySection repo={repo} health={health} />

      <ActivityTimeline />

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
                    {kindBadge(k)}
                  </option>
                ))}
              </select>
            </label>
            <label className="row-meta" htmlFor="history-search">
              Search{' '}
              <input
                id="history-search"
                data-testid="history-search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="summary, detail, or kind"
              />
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
        {loading && <div data-testid="history-loading" role="status">Loading history…</div>}
        {!loading && !error && (
          <RowsList rows={rows} filtering={filtering} onRefresh={() => void load()} />
        )}
      </section>
    </div>
  );
}
