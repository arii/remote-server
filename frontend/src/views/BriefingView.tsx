import { useEffect, useState } from 'react';
import { fetchBriefing, fetchLedger } from '../apiClient';
import type { BoardItem, BriefingResponse, LedgerEntry } from '../apiTypes';
import { toHistoryRows } from './historyUtils';
import { loadBriefing, loadHistory, useMock } from './mockViews';

// Briefing lines come from the legacy briefing_summary passthrough and may
// carry reviewer HTML-comment markers, markdown list markers, or run long.
// Present the human part.
export function cleanBriefingLine(line: string): string {
  const stripped = line
    .replace(/<!--[\s\S]*?-->/g, ' ')
    .replace(/ai-review-state:\s*\S+/g, ' ')
    .replace(/<!--[\s\S]*$/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/^[-*]\s+/, '');
  return stripped.length > 280 ? `${stripped.slice(0, 277)}…` : stripped;
}

function Lines({ lines }: { lines: string[] }) {
  return (
    <ul className="session-list">
      {lines.map((line, i) => (
        // eslint-disable-next-line react/no-array-index-key
        <li key={i} className="detail">
          <p>{cleanBriefingLine(line)}</p>
        </li>
      ))}
    </ul>
  );
}

function Section({ title, lines, err }: { title: string; lines: string[]; err?: string }) {
  if (lines.length === 0 && !err) return null;
  return (
    <section>
      <h3>{title}</h3>
      {err && (
        <p className="row-meta" role="alert">
          {err}
        </p>
      )}
      {lines.length === 1 && !err ? (
        <p>{cleanBriefingLine(lines[0])}</p>
      ) : (
        <Lines lines={lines} />
      )}
    </section>
  );
}

function BoardSection({
  board,
  lines,
  err,
}: {
  board: BoardItem[];
  lines: string[];
  err?: string;
}) {
  if (board.length === 0) {
    return <Section title="Board" lines={lines} err={err} />;
  }
  return (
    <section>
      <h3>Board</h3>
      {err && (
        <p className="row-meta" role="alert">
          {err}
        </p>
      )}
      <ul className="session-list" data-testid="board-chips">
        {board.map((b, i) => {
          const chips = [b.branch, b.issue ? `issue ${b.issue}` : '', b.status]
            .map((v) => (v ?? '').trim())
            .filter((v) => v.length > 0 && v !== '?');
          // eslint-disable-next-line react/no-array-index-key
          return (
            <li key={i} className="detail">
              <div className="chip-row">
                {chips.map((c) => (
                  <span key={c} className="repo-chip">
                    {c}
                  </span>
                ))}
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

// One-line condensation of briefing state, always visible.
export function briefingSummary(data: BriefingResponse): string {
  if (briefingAllClear(data)) return 'All clear — nothing open.';
  const parts: string[] = [];
  if (data.prs.length > 0) {
    parts.push(`${data.prs.length} open PR${data.prs.length === 1 ? '' : 's'}`);
  }
  if (data.attention.length > 0) {
    parts.push(`${data.attention.length} flagged`);
  }
  if (data.conflicts_lines.length > 0) {
    parts.push(
      `${data.conflicts_lines.length} conflict${data.conflicts_lines.length === 1 ? '' : 's'}`,
    );
  }
  if (data.workflow_lines.length > 0) {
    parts.push(
      `${data.workflow_lines.length} failing run${data.workflow_lines.length === 1 ? '' : 's'}`,
    );
  }
  return parts.join(' · ');
}

export interface BriefingAction {
  label: string;
  seed: string;
}

// Next actions derived from state, most urgent first, capped at three.
// Each seeds agent chat, which auto-sends.
export function briefingActions(data: BriefingResponse): BriefingAction[] {
  const actions: BriefingAction[] = [];
  if (data.workflow_lines.length > 0) {
    actions.push({
      label: 'Explain the failures',
      seed: 'What failed recently and how do I fix it?',
    });
  }
  if (data.conflicts_lines.length > 0) {
    actions.push({
      label: 'Resolve the conflicts',
      seed: 'How do I resolve these merge conflicts?',
    });
  }
  if (data.prs.length > 0) {
    actions.push({
      label: 'Summarize open PRs',
      seed: 'Summarize the open pull requests.',
    });
  }
  if (data.attention.length > 0) {
    actions.push({
      label: 'Triage attention items',
      seed: 'Help me triage these attention items.',
    });
  }
  if (actions.length === 0) {
    actions.push({
      label: 'Plan my day',
      seed: 'What should I work on next?',
    });
  }
  return actions.slice(0, 3);
}

// 24h activity counts from ledger timestamps.
export function dayTrend(entries: LedgerEntry[], nowSec: number): string {
  const recent = entries.filter(
    (e) => typeof e.ts === 'number' && e.ts >= nowSec - 86400,
  );
  const failures = recent.filter((e) => e.ok === false).length;
  return `Last 24h: ${recent.length} event${recent.length === 1 ? '' : 's'}, ${failures} failure${failures === 1 ? '' : 's'}.`;
}

// Condense-empties: when every section is empty and nothing errored,
// the view shows one all-clear instead of five bare headings.
export function briefingAllClear(data: BriefingResponse): boolean {
  if (data.prs_err || data.board_err || data.runs_err) return false;
  return (
    data.prs.length === 0 &&
    data.attention.length === 0 &&
    data.reviews_lines.length === 0 &&
    (data.board ?? []).length === 0 &&
    data.board_lines.length === 0 &&
    data.conflicts_lines.length === 0 &&
    data.overlaps_lines.length === 0 &&
    data.workflow_lines.length === 0
  );
}

function PrRow({ pr }: { pr: Record<string, unknown> }) {
  const meta = [pr.author, pr.branch, pr.ciLabel ?? pr.ciStatus]
    .map((v) => String(v ?? '').trim())
    .filter((v) => v.length > 0);
  return (
    <li className="detail">
      <span className="row-title">
        #{String(pr.number ?? '?')} {String(pr.title ?? 'Untitled')}
      </span>
      {meta.length > 0 && <p className="row-meta">{meta.join(' · ')}</p>}
    </li>
  );
}

export default function BriefingView({
  repo,
  onAskAgent,
  onOpenHistory,
}: {
  repo: string;
  onAskAgent: (seed: string) => void;
  onOpenHistory: () => void;
}) {
  const [data, setData] = useState<BriefingResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ledger, setLedger] = useState<LedgerEntry[] | null>(null);

  async function load() {
    setError(null);
    try {
      setData(await fetchBriefing(repo));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  useEffect(() => {
    let cancelled = false;
    setData(null);
    setError(null);
    setLedger(null);
    if (useMock()) {
      setData(loadBriefing());
      setLedger(loadHistory().entries);
      return;
    }
    (async () => {
      try {
        const live = await fetchBriefing(repo);
        if (!cancelled) setData(live);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
    })();
    // Secondary: recent-activity highlights. Failure degrades to the
    // empty state instead of breaking the briefing.
    (async () => {
      try {
        const live = await fetchLedger('all', 100);
        if (!cancelled) setLedger(live.entries);
      } catch {
        if (!cancelled) setLedger([]);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repo]);

  if (!data && !error) return <section data-testid="briefing-loading" role="status">Loading…</section>;

  return (
    <section data-testid="briefing-view">
      <header className="page-head">
        <div>
          <h2>Briefing</h2>
          <p className="row-meta">Repo: {data?.repo || repo}</p>
        </div>
        {error && (
          <div className="actions">
            <button type="button" data-testid="briefing-retry" onClick={() => void load()}>
              Retry
            </button>
          </div>
        )}
      </header>
      {error && (
        <div data-testid="briefing-error" role="alert">
          Briefing unavailable — {error}
        </div>
      )}
      {data && (
        <BriefingBody
          data={data}
          ledger={ledger}
          onAskAgent={onAskAgent}
          onOpenHistory={onOpenHistory}
        />
      )}
    </section>
  );
}

export function BriefingBody({
  data,
  ledger,
  onAskAgent,
  onOpenHistory,
}: {
  data: BriefingResponse;
  ledger: LedgerEntry[] | null;
  onAskAgent: (seed: string) => void;
  onOpenHistory: () => void;
}) {
  const actions = briefingActions(data);
  const highlights = toHistoryRows([], ledger ?? []).slice(0, 3);
  return (
    <>
      <p className="row-meta" data-testid="briefing-summary">
        {briefingSummary(data)}
      </p>
      {data.prs_err && (
        <p className="row-meta" role="alert">
          {data.prs_err}
        </p>
      )}
      {briefingAllClear(data) ? (
        <div data-testid="briefing-clear">
          <h3>All clear</h3>
          <p className="row-meta">No open PRs, no conflicts, no failing runs.</p>
        </div>
      ) : (
        <>
          <Section title="Needs attention" lines={data.attention} />
          {data.prs.length > 0 && (
            <section>
              <h3>Open pull requests</h3>
              <ul className="session-list">
                {data.prs.map((pr, i) => (
                  // eslint-disable-next-line react/no-array-index-key
                  <PrRow key={i} pr={pr} />
                ))}
              </ul>
            </section>
          )}
          <Section title="Reviews" lines={data.reviews_lines} />
          <BoardSection board={data.board ?? []} lines={data.board_lines} err={data.board_err} />
          <Section title="Conflicts" lines={data.conflicts_lines} />
          <Section title="Overlaps" lines={data.overlaps_lines} />
          <Section title="Failing runs" lines={data.workflow_lines} err={data.runs_err} />
        </>
      )}
      <section aria-label="Next actions">
        <h3>Next actions</h3>
        <div className="actions" data-testid="briefing-actions">
          {actions.map((a) => (
            <button key={a.label} type="button" onClick={() => onAskAgent(a.seed)}>
              {a.label}
            </button>
          ))}
        </div>
      </section>
      <section aria-label="Recent activity">
        <h3>Recent activity</h3>
        {highlights.length > 0 ? (
          <ul className="session-list" data-testid="briefing-highlights">
            {highlights.map((r) => (
              <li key={r.key} className="detail">
                <span className="row-title">{r.summary}</span>
                <p className="row-meta">
                  {r.kind} · {r.at}
                </p>
              </li>
            ))}
          </ul>
        ) : (
          <p className="row-meta">No recent activity to show.</p>
        )}
        <p className="row-meta">{dayTrend(ledger ?? [], Date.now() / 1000)}</p>
        <div className="actions">
          <button type="button" data-testid="briefing-history" onClick={onOpenHistory}>
            Full log in History
          </button>
        </div>
      </section>
    </>
  );
}
