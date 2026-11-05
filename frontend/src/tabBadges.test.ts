import { describe, expect, it } from 'vitest';
import type { InboxFeed } from './apiClient';
import type { DispatchItem, JobRecord } from './apiTypes';
import type { PrRow } from './reviewTypes';
import {
  EMPTY_BADGE_COUNTS,
  historyErrorCount,
  jobsActiveCount,
  navBadge,
  nowBadgeCount,
  queueActiveCount,
  reviewBadgeCount,
} from './tabBadges';

// Live nav-badge counts (issue #936): every counter derives from the
// same payload its tab renders. Null means not loaded yet — the pill
// stays hidden instead of flashing a wrong zero.

function feedWith(n: number): InboxFeed {
  return { attention: Array.from({ length: n }, (_, i) => ({ id: `a${i}` })) } as InboxFeed;
}

function prs(n: number): PrRow[] {
  return Array.from({ length: n }, (_, i) => ({ number: i + 1, title: `pr ${i}` })) as PrRow[];
}

describe('nowBadgeCount', () => {
  it('counts inbox attention items', () => {
    expect(nowBadgeCount(feedWith(3))).toBe(3);
  });

  it('is null until the feed loads', () => {
    expect(nowBadgeCount(null)).toBeNull();
    expect(nowBadgeCount(undefined)).toBeNull();
  });
});

describe('reviewBadgeCount', () => {
  it('counts open pull requests', () => {
    expect(reviewBadgeCount(prs(2))).toBe(2);
  });

  it('is null until PRs load', () => {
    expect(reviewBadgeCount(null)).toBeNull();
  });
});

describe('historyErrorCount', () => {
  it('is null until both feeds load', () => {
    const jobs: JobRecord[] = [{ id: 'j1', done: true }];
    expect(historyErrorCount(null, [])).toBeNull();
    expect(historyErrorCount(jobs, null)).toBeNull();
    expect(historyErrorCount(jobs, undefined)).toBeNull();
  });

  it('counts zero errors on empty feeds', () => {
    expect(historyErrorCount([], [])).toBe(0);
  });
});

describe('jobsActiveCount', () => {
  it('counts runs that are not done', () => {
    const jobs: JobRecord[] = [
      { id: 'a', done: false },
      { id: 'b', done: true },
      { id: 'c' },
    ];
    expect(jobsActiveCount(jobs)).toBe(2);
  });

  it('is null until jobs load', () => {
    expect(jobsActiveCount(null)).toBeNull();
  });
});

describe('queueActiveCount', () => {
  it('counts queued and running dispatch items', () => {
    const items: DispatchItem[] = [
      { repo: 'r', issue: 1, state: 'queued' },
      { repo: 'r', issue: 2, state: 'running' },
      { repo: 'r', issue: 3, state: 'done' },
    ];
    expect(queueActiveCount(items)).toBe(2);
  });

  it('is null until the queue loads', () => {
    expect(queueActiveCount(null)).toBeNull();
  });
});

describe('navBadge', () => {
  it('hides unknown and zero counts', () => {
    expect(navBadge('now', EMPTY_BADGE_COUNTS)).toBeNull();
    expect(navBadge('chat', { ...EMPTY_BADGE_COUNTS, now: 5 })).toBeNull();
    expect(navBadge('review', { ...EMPTY_BADGE_COUNTS, review: 0 })).toBeNull();
    expect(navBadge('unknown-tab', { ...EMPTY_BADGE_COUNTS, now: 5 })).toBeNull();
  });

  it('shows the count with an accessible label', () => {
    expect(navBadge('now', { ...EMPTY_BADGE_COUNTS, now: 2 })).toEqual({
      count: 2,
      label: '2 needing attention',
    });
    expect(navBadge('review', { ...EMPTY_BADGE_COUNTS, review: 1 })).toEqual({
      count: 1,
      label: '1 open pull request',
    });
    expect(navBadge('history', { ...EMPTY_BADGE_COUNTS, history: 4 })).toEqual({
      count: 4,
      label: '4 errors',
    });
    expect(navBadge('jobs', { ...EMPTY_BADGE_COUNTS, jobs: 1 })).toEqual({
      count: 1,
      label: '1 active run',
    });
    expect(navBadge('queue', { ...EMPTY_BADGE_COUNTS, queue: 3 })).toEqual({
      count: 3,
      label: '3 queued or running',
    });
  });
});
