import { useEffect } from 'react';
import { fetchAgentRuns, fetchJobs, fetchLedger } from '../apiClient';
import type { AgentRun } from '../apiClient';
import type { JobRecord, LedgerEntry } from '../apiTypes';
import { loadHistory, useMock } from './mockViews';
import { isTabVisible, onTabVisible } from './jobsUtils';
import { useSnapshotData } from './useSnapshotData';
import { StatusIcon } from './StatusIcon';
import { toTimelineItems } from './timelineUtils';
import type { TimelineItem } from './timelineUtils';

// Timeline refresh cadence: slow enough to never storm (four
// feeds per refresh), fast enough that new activity lands
// without a manual tap. Same hidden-tab gating as Jobs polling.
export const TIMELINE_POLL_MS = 15_000;
const TIMELINE_LIMIT = 50;

interface TimelineFeeds {
  jobs: JobRecord[];
  turns: AgentRun[];
  pulls: LedgerEntry[];
  merges: LedgerEntry[];
}

const EMPTY_FEEDS: TimelineFeeds = { jobs: [], turns: [], pulls: [], merges: [] };

async function loadTimelineFeeds(): Promise<TimelineFeeds> {
  if (useMock()) {
    const h = loadHistory();
    return {
      jobs: h.jobs,
      turns: [],
      pulls: h.entries.filter((e) => e.kind === 'webhook'),
      merges: h.entries.filter((e) => e.kind === 'pr-action'),
    };
  }
  const [j, t, p, m] = await Promise.all([
    fetchJobs(),
    fetchAgentRuns(TIMELINE_LIMIT),
    fetchLedger('webhook', TIMELINE_LIMIT),
    fetchLedger('pr-action', TIMELINE_LIMIT),
  ]);
  return { jobs: j.jobs, turns: t.runs, pulls: p.entries, merges: m.entries };
}

// Pure list for tests and preview harnesses. Rows reuse the
// History row classes verbatim (same icon, same title/meta
// slots), so the timeline refines the incumbent look instead
// of introducing a new one.
export function TimelineList({ items }: { items: TimelineItem[] }) {
  if (items.length === 0) {
    return (
      <p className="row-meta" data-testid="timeline-empty">
        No activity yet.
      </p>
    );
  }
  return (
    <ul className="session-list" data-testid="timeline-list">
      {items.map((item) => (
        <li
          key={item.key}
          className="history-row"
          data-testid={`timeline-item-${item.feed}`}
        >
          <span className="row-title">
            <StatusIcon tone={item.tone} /> {item.summary}
          </span>
          <span className="row-meta">
            {item.feed} · {item.at}
          </span>
        </li>
      ))}
    </ul>
  );
}

export default function ActivityTimeline({ active = true }: { active?: boolean }) {
  const { data: feeds, error, loading, load } = useSnapshotData(loadTimelineFeeds, EMPTY_FEEDS);

  useEffect(() => {
    if (useMock() || !active) return;
    const timer = window.setInterval(() => {
      if (isTabVisible()) void load();
    }, TIMELINE_POLL_MS);
    const stopResume = onTabVisible(() => void load());
    return () => {
      window.clearInterval(timer);
      stopResume();
    };
  }, [load, active]);

  const items = toTimelineItems(feeds.jobs, feeds.turns, feeds.pulls, feeds.merges);

  return (
    <section aria-label="Activity" data-testid="timeline-view">
      <div className="page-head">
        <div>
          <h2>Activity</h2>
          <p className="row-meta">Runs, turns, pulls, and merges, newest first.</p>
        </div>
        <div className="actions" role="group" aria-label="Activity actions">
          <button type="button" data-testid="timeline-refresh" onClick={() => void load()}>
            Refresh
          </button>
        </div>
      </div>

      {error && (
        <div data-testid="timeline-error" role="alert">
          Activity unavailable: {error}
        </div>
      )}
      {loading && <div data-testid="timeline-loading" role="status">Loading activity…</div>}
      {!loading && !error && <TimelineList items={items} />}
    </section>
  );
}
