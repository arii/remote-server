import { useEffect, useRef, useState } from 'react';
import CopyButton from '../components/CopyButton';
import RetryableError from '../components/RetryableError';
import { fetchJobRun, fetchJobs, fetchLedger, rerunJob } from '../apiClient';
import type { JobRecord, LedgerEntry } from '../apiTypes';
import { loadHistory, useMock } from './mockViews';
import {
  HISTORY_FACETS,
  facetCounts,
  historyRowMatches,
  jobPassesKindFilter,
  kindBadge,
  rowInFacet,
  tailText,
  toHistoryRows,
  visibleFacets,
} from './historyUtils';
import type { HistoryFacet, HistoryMember, HistoryRow } from './historyUtils';
import { WorkflowRunsSection } from './ObservabilitySection';
import ActivityTimeline from './ActivityTimeline';
import { shouldShowSnapshotContent, useSnapshotData } from './useSnapshotData';
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

// Skipped-webhook noise filter (#964): non-failure rows whose
// summary names a skip hide by default behind a toggle. Failed
// rows never count as skips — they always stay visible.
export function isSkipRow(r: HistoryRow): boolean {
  return r.tone !== 'bad' && /skipped/i.test(r.summary);
}

// Agent-run noise filter (#1014): failure-free agent-turn/cli-turn
// rows hide by default behind their own toggle (agent sessions are
// frequent and verbose; only their failures earn feed space).
// Failed agent rows always stay visible, same rule as skips.
export const AGENT_ROW_KINDS = new Set(['agent-turn', 'cli-turn']);
export function isQuietAgentRow(r: HistoryRow): boolean {
  return r.tone !== 'bad' && AGENT_ROW_KINDS.has(r.kind);
}

// Inline status edge (#964): green/red left edge per row, readable
// without expanding. Muted rows carry no edge.
export function rowToneClass(tone: HistoryRow['tone']): string {
  if (tone === 'bad' || tone === 'ok') return ` tone-${tone}`;
  return '';
}

// Lazy run-output disclosure, same onToggle pattern as the review diff:
// fetch only when opened. The runs registry is in-memory, so output for
// old runs 404s — surfaced as a plain note, not a crash.
export function RunOutput({ runId }: { runId: string }) {
  const [output, setOutput] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const mock = useMock();

  async function fetchOutput() {
    try {
      const p = await fetchJobRun(runId);
      setOutput(p.output || '');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function load() {
    if (output !== null || error || mock) return;
    await fetchOutput();
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
        <RetryableError
          testid={`history-run-output-error-${runId}`}
          message={`Couldn't load run output: ${error}`}
          retryTestid={`history-run-output-retry-${runId}`}
          onRetry={() => {
            setError(null);
            void fetchOutput();
          }}
        />
      )}
      {!mock && !error && output !== null && (
        <>
          <div className="actions">
            <CopyButton
              text={output}
              testid={`history-run-output-copy-${runId}`}
              label="Copy output"
            />
          </div>
          <pre className="row-meta" data-testid={`history-run-output-body-${runId}`}>
            {tailText(output) || 'No output recorded.'}
          </pre>
        </>
      )}
      {!mock && !error && output === null && (
        <p className="row-meta" data-testid={`history-run-output-loading-${runId}`} role="status">
          Loading run output…
        </p>
      )}
    </details>
  );
}

export function MemberBlock({ member, grouped }: { member: HistoryMember; grouped: boolean }) {
  const text = grouped ? `${member.at}\n${member.detail}` : member.detail;
  return (
    <>
      <div className="actions">
        <CopyButton text={text} testid="history-group-copy" label="Copy entry" />
      </div>
      <pre className="row-meta" data-testid="history-group-entry">
        {text}
      </pre>
      {member.runId && <RunOutput runId={member.runId} />}
    </>
  );
}

// Inspector rail (issue #839): the selected row with full logs
// per member plus Re-run where a member came from a job run.
// Re-run replays the run's own action/repo/argv via the same
// endpoint Jobs uses; mock mode hides it (no live registry).
export function HistoryInspector({
  row,
  canRerun,
  rerunBusy,
  rerunNote,
  onRerun,
}: {
  row: HistoryRow | null;
  canRerun: boolean;
  rerunBusy: string | null;
  rerunNote: string | null;
  onRerun: (runId: string) => void;
}) {
  if (!row) {
    return (
      <div className="detail" data-testid="history-inspector">
        <span className="row-title">No row selected</span>
        <p className="row-meta">Select a feed row to inspect its full logs.</p>
      </div>
    );
  }
  return (
    <div className="detail" data-testid="history-inspector">
      <span className="row-title">
        <StatusIcon tone={row.tone} /> {row.summary}
      </span>
      <p className="row-meta">
        {kindBadge(row.kind)} · {row.at}
        {row.repeat > 1 ? ` · ×${row.repeat}` : ''}
        {row.recovery && <> · Recovery: {row.recovery}</>}
      </p>
      {rerunNote && (
        <p className="row-meta" data-testid="history-rerun-note" role="status">
          {rerunNote}
        </p>
      )}
      {row.members.map((m, mi) => (
        // eslint-disable-next-line react/no-array-index-key
        <div key={mi}>
          <MemberBlock member={m} grouped={row.members.length > 1} />
          {canRerun && m.runId && (
            <div className="actions">
              <button
                type="button"
                data-testid={`history-rerun-${m.runId}`}
                disabled={rerunBusy !== null}
                onClick={() => onRerun(m.runId as string)}
              >
                {rerunBusy === m.runId ? 'Re-running…' : 'Re-run'}
              </button>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

export function RowsList({
  rows,
  filtering,
  selectedKey = null,
  onSelect = () => {},
  onRefresh = () => {},
  onClearFilters = () => {},
}: {
  rows: HistoryRow[];
  filtering: boolean;
  selectedKey?: string | null;
  onSelect?: (row: HistoryRow) => void;
  onRefresh?: () => void;
  onClearFilters?: () => void;
}) {
  if (rows.length === 0) {
    if (filtering) {
      return (
        <p className="row-meta" data-testid="history-empty">
          No matching rows.{' '}
          <button type="button" data-testid="history-clear-filters" onClick={onClearFilters}>
            Clear search &amp; filters
          </button>
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
            className={`history-row${selectedKey === r.key ? ' selected' : ''}${rowToneClass(r.tone)}`}
            data-testid={r.repeat > 1 ? 'history-incident' : 'history-row'}
            aria-current={selectedKey === r.key ? true : undefined}
          >
            <details data-testid="history-detail">
              <summary onClick={() => onSelect(r)}>
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

// Cross-tab feed filter (issue #931): a container pill in Now or
// the Status drawer navigates here pre-filtered to that service.
// Each nonce applies once, including on a fresh mount after the
// tab switch (same path as Now's palette section requests).
export interface HistoryFilterRequest {
  query?: string;
  facet?: HistoryFacet;
  nonce: number;
}

// Active-filter chip: names the query + facet narrowing the feed
// with one tap to clear both. Null when the feed is unfiltered.
export function HistoryFilterChip({
  query,
  facet,
  onClear,
}: {
  query: string;
  facet: HistoryFacet;
  onClear: () => void;
}) {
  if (!query.trim() && facet === 'all') return null;
  const parts: string[] = [];
  if (query.trim()) parts.push(`\u201c${query.trim()}\u201d`);
  if (facet !== 'all') {
    parts.push(HISTORY_FACETS.find((f) => f.id === facet)?.label ?? facet);
  }
  return (
    <div className="actions" data-testid="history-filter-chip" role="status">
      <span className="row-meta">Filter: {parts.join(' · ')}</span>{' '}
      <button type="button" data-testid="history-filter-clear" onClick={onClear}>
        Clear
      </button>
    </div>
  );
}

export default function HistoryView({
  repo,
  active = true,
  queryRequest = null,
  filterRequest = null,
}: {
  repo: string;
  // Keep-alive visibility (issue #897): the timeline poll pauses
  // while the tab is hidden.
  active?: boolean;
  // Deep-link search seed (issue #930): a nonce bump from App
  // re-seeds the search box — the Now hero's deploy segment lands
  // here filtered to deploy runs.
  queryRequest?: { query: string; nonce: number } | null;
  // Cross-tab feed filter (issue #931): a nonce bump from a Now
  // container pill or the Status drawer pre-filters the feed.
  filterRequest?: HistoryFilterRequest | null;
}) {
  const { data, error, loading, load } = useSnapshotData(loadHistorySnapshot, EMPTY_HISTORY);
  const { jobs, entries } = data;
  const [kind, setKind] = useState('all');
  const [query, setQuery] = useState('');
  const [facet, setFacet] = useState<HistoryFacet>('all');
  // Skipped webhooks hide by default (#964): the feed buries
  // signal in repeated low-value skip rows.
  const [showSkips, setShowSkips] = useState(false);
  // Failure-free agent runs hide by default (#1014): same rule as
  // skips, own toggle so failures still surface either way.
  const [showAgent, setShowAgent] = useState(false);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  // Deep-link seeds land even on a live view (keepalive): each
  // nonce applies once and resets the other filters, so the link
  // owns the full filter state instead of intersecting it.
  const handledQueryNonce = useRef(0);
  useEffect(() => {
    if (queryRequest && queryRequest.nonce !== handledQueryNonce.current) {
      handledQueryNonce.current = queryRequest.nonce;
      setQuery(queryRequest.query);
      setKind('all');
      setFacet('all');
      setSelectedKey(null);
    }
  }, [queryRequest]);
  const [rerunBusy, setRerunBusy] = useState<string | null>(null);
  const [rerunNote, setRerunNote] = useState<string | null>(null);

  const handledFilterNonce = useRef(0);
  useEffect(() => {
    if (filterRequest && filterRequest.nonce !== handledFilterNonce.current) {
      handledFilterNonce.current = filterRequest.nonce;
      if (filterRequest.query !== undefined) setQuery(filterRequest.query);
      if (filterRequest.facet !== undefined) setFacet(filterRequest.facet);
    }
  }, [filterRequest]);
  const all = entries;
  const filtering = query.trim().length > 0 || facet !== 'all';
  const scoped = toHistoryRows(
    jobs.filter((j) => jobPassesKindFilter(j.kind, kind)),
    filterByKind(all, kind),
  ).filter((r) => historyRowMatches(r, query));
  const counts = facetCounts(scoped);
  const rows = scoped.filter((r) => rowInFacet(r, facet));
  // Identical consecutive skips already fold into one expandable ×N
  // row upstream (#855); here they hide behind the toggle instead
  // of burying the feed. Failures never hide (see isSkipRow).
  const visibleRows = rows.filter(
    (r) =>
      (showSkips || !isSkipRow(r)) &&
      (showAgent || !isQuietAgentRow(r)),
  );
  // Per-toggle reveal counts: flipping one toggle surfaces exactly
  // the rows its own filter hides (rows hidden by both toggles need
  // both flips and sit out of both counts).
  const hiddenSkips = rows.filter(
    (r) => !showSkips && isSkipRow(r) &&
      (showAgent || !isQuietAgentRow(r)),
  ).length;
  const hiddenAgents = rows.filter(
    (r) => !showAgent && isQuietAgentRow(r) &&
      (showSkips || !isSkipRow(r)),
  ).length;
  // The inspector follows the selection, defaulting to the newest
  // row; a selection that filters out falls back the same way.
  const inspected =
    visibleRows.find((r) => r.key === selectedKey) ?? visibleRows[0] ?? null;

  async function onRerun(runId: string) {
    if (rerunBusy !== null) return;
    setRerunBusy(runId);
    setRerunNote(null);
    try {
      const p = await fetchJobRun(runId);
      const started = await rerunJob(p.action, p.repo ?? repo, p.td_argv ?? [], true);
      setRerunNote(`Re-run started: ${started.id}`);
      await load();
    } catch (err) {
      setRerunNote(
        `Re-run failed: ${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      setRerunBusy(null);
    }
  }

  // Stale-first (#907): rows stay visible during reloads with
  // an inline Refreshing note; the full loader shows only when
  // nothing has rendered yet. Errors never hide stale rows.
  const initial = jobs.length === 0 && entries.length === 0;
  const showContent = shouldShowSnapshotContent(initial, loading);

  function clearFilters() {
    setQuery('');
    setFacet('all');
    setKind('all');
    setShowSkips(true);
    setShowAgent(true);
  }

  return (
    <div data-testid="history-view">
      <WorkflowRunsSection repo={repo} onFindInFeed={(text) => setQuery(text)} />

      <ActivityTimeline active={active} />

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
            <label className="row-meta" htmlFor="history-show-skips">
              <input
                id="history-show-skips"
                type="checkbox"
                data-testid="history-show-skips"
                checked={showSkips}
                onChange={(e) => setShowSkips(e.target.checked)}
              />{' '}
              Skips
            </label>
            <label className="row-meta" htmlFor="history-show-agent">
              <input
                id="history-show-agent"
                type="checkbox"
                data-testid="history-show-agent"
                checked={showAgent}
                onChange={(e) => setShowAgent(e.target.checked)}
              />{' '}
              Agent runs
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
        {loading && initial && <div data-testid="history-loading" role="status">Loading history…</div>}
        {loading && !initial && (
          <div data-testid="history-refreshing" role="status" className="row-meta">
            Refreshing…
          </div>
        )}
        {showContent && (
          <>
            <HistoryFilterChip
              query={query}
              facet={facet}
              onClear={() => {
                setQuery('');
                setFacet('all');
              }}
            />
            <div
              className="facet-row"
              role="group"
              aria-label="Event facets"
              data-testid="history-facets"
            >
              {visibleFacets(counts).map((f) => (
                <button
                  key={f.id}
                  type="button"
                  data-testid={`history-facet-${f.id}`}
                  aria-pressed={facet === f.id}
                  onClick={() => setFacet(f.id)}
                >
                  {f.label} {counts[f.id]}
                </button>
              ))}
            </div>
            {hiddenSkips > 0 && (
              <p className="row-meta" data-testid="history-skips-note">
                {hiddenSkips} skipped webhook{hiddenSkips === 1 ? '' : 's'} hidden.{' '}
                <button type="button" data-testid="history-skips-show" onClick={() => setShowSkips(true)}>
                  Show
                </button>
              </p>
            )}
            {hiddenAgents > 0 && (
              <p className="row-meta" data-testid="history-agent-note">
                {hiddenAgents} agent run{hiddenAgents === 1 ? '' : 's'} hidden.{' '}
                <button type="button" data-testid="history-agent-show" onClick={() => setShowAgent(true)}>
                  Show
                </button>
              </p>
            )}
            <div className="split">
              <div>
                <RowsList
                  rows={visibleRows}
                  filtering={filtering || hiddenSkips > 0 || hiddenAgents > 0}
                  selectedKey={inspected?.key ?? null}
                  onSelect={(r) => setSelectedKey(r.key)}
                  onRefresh={() => void load()}
                  onClearFilters={clearFilters}
                />
              </div>
              <aside className="inspector" aria-label="Row inspector">
                <HistoryInspector
                  row={inspected}
                  canRerun={!useMock()}
                  rerunBusy={rerunBusy}
                  rerunNote={rerunNote}
                  onRerun={(runId) => void onRerun(runId)}
                />
              </aside>
            </div>
          </>
        )}
      </section>
    </div>
  );
}
