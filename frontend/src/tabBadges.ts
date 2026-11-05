import type { InboxFeed } from './apiClient';
import type { DispatchItem, JobRecord, LedgerEntry } from './apiTypes';
import type { PrRow } from './reviewTypes';
import { facetCounts, toHistoryRows } from './views/historyUtils';

// Live nav-badge counts (issue #936): every counter derives from the
// same payload its tab renders, fetched at the App level so the nav
// answers "what needs me where?" without opening each tab. Null means
// "not loaded yet" (or the last refresh failed before any success):
// the badge stays hidden instead of flashing a wrong zero, and a
// failed refresh keeps the last good count.

export interface NavBadgeCounts {
  now: number | null;
  review: number | null;
  history: number | null;
  jobs: number | null;
  queue: number | null;
}

export const EMPTY_BADGE_COUNTS: NavBadgeCounts = {
  now: null,
  review: null,
  history: null,
  jobs: null,
  queue: null,
};

export type BadgeEntryId = 'now' | 'review' | 'history' | 'jobs' | 'queue';

export interface NavBadge {
  count: number;
  /** Accessible description, e.g. "2 needing attention". */
  label: string;
}

/** Now badge: inbox items needing the operator. */
export function nowBadgeCount(feed: InboxFeed | null | undefined): number | null {
  return feed ? feed.attention.length : null;
}

/** Review badge: open pull requests for the viewed repo. */
export function reviewBadgeCount(prs: readonly PrRow[] | null | undefined): number | null {
  return prs ? prs.length : null;
}

/**
 * History badge: error rows in the activity feed, counted exactly as
 * the History view's Errors facet counts them (collapsed rows, tone
 * bad), so the pill and the facet chip never disagree. Null until
 * both feeds have loaded once.
 */
export function historyErrorCount(
  jobs: readonly JobRecord[] | null | undefined,
  entries: readonly LedgerEntry[] | null | undefined,
): number | null {
  if (!jobs || !entries) return null;
  return facetCounts(toHistoryRows([...jobs], [...entries])).errors;
}

/** Jobs badge: runs still active (not done). */
export function jobsActiveCount(jobs: readonly JobRecord[] | null | undefined): number | null {
  return jobs ? jobs.filter((j) => !j.done).length : null;
}

/** Queue badge: dispatch items waiting or in flight. */
export function queueActiveCount(
  items: readonly DispatchItem[] | null | undefined,
): number | null {
  return items
    ? items.filter((i) => i.state === 'queued' || i.state === 'running').length
    : null;
}

function plural(count: number, one: string, many: string): string {
  return count === 1 ? one : many;
}

const BADGE_LABELS: Record<BadgeEntryId, (count: number) => string> = {
  now: (n) => `${n} needing attention`,
  review: (n) => `${n} open ${plural(n, 'pull request', 'pull requests')}`,
  history: (n) => `${n} ${plural(n, 'error', 'errors')}`,
  jobs: (n) => `${n} active ${plural(n, 'run', 'runs')}`,
  queue: (n) => `${n} queued or running`,
};

/**
 * Badge for one nav entry, or null when it stays hidden: unknown
 * counts and zero counts render nothing, so the bar only speaks up
 * when a tab actually needs the operator. Chat and Briefing carry no
 * queue count — their payloads are precached (instant switches), but
 * nothing countable waits there.
 */
export function navBadge(entryId: string, counts: NavBadgeCounts): NavBadge | null {
  if (
    entryId !== 'now' &&
    entryId !== 'review' &&
    entryId !== 'history' &&
    entryId !== 'jobs' &&
    entryId !== 'queue'
  ) {
    return null;
  }
  const count = counts[entryId];
  if (count === null || count <= 0) return null;
  return { count, label: BADGE_LABELS[entryId](count) };
}
