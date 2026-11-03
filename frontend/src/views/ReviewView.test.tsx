import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import {
  IssueDetails,
  applyOptimisticMerge,
  isRowDeferred,
  nextPrAfterMerge,
  revertOptimisticMerge,
} from './ReviewView';
import type { IssueRow } from '../reviewTypes';

describe('optimistic merge row state', () => {
  it('applies the pending state without mutating the prior set', () => {
    const prev = new Set([7]);
    const next = applyOptimisticMerge(prev, 12);
    expect(next.has(12)).toBe(true);
    expect(prev.has(12)).toBe(false);
  });

  it('restores the exact prior state on failure', () => {
    const prev = new Set([7]);
    const pending = applyOptimisticMerge(prev, 12);
    expect(revertOptimisticMerge(pending, 12)).toEqual(prev);
    expect(revertOptimisticMerge(pending, 12).has(12)).toBe(false);
  });

  it('leaves unrelated rows untouched', () => {
    const pending = applyOptimisticMerge(new Set([7, 9]), 12);
    expect([...pending].sort((a, b) => a - b)).toEqual([7, 9, 12]);
    expect([...revertOptimisticMerge(pending, 12)].sort((a, b) => a - b)).toEqual([7, 9]);
  });
});

describe('next PR after merge', () => {
  it('advances to the next row when the merged PR is in the middle', () => {
    expect(nextPrAfterMerge([3, 7, 9], 7)).toBe(9);
  });

  it('wraps to the first row when the merged PR was last', () => {
    expect(nextPrAfterMerge([3, 7, 9], 9)).toBe(3);
  });

  it('returns null when there is no other PR', () => {
    expect(nextPrAfterMerge([7], 7)).toBeNull();
  });

  it('returns null when the merged PR is not in the list', () => {
    expect(nextPrAfterMerge([3, 7, 9], 12)).toBeNull();
  });
});

describe('deferred row matching', () => {
  const deferred = [
    { kind: 'pr', number: 7 },
    { kind: 'issue', number: 3 },
  ];

  it('matches on kind and number together', () => {
    expect(isRowDeferred(deferred, 'pr', 7)).toBe(true);
    expect(isRowDeferred(deferred, 'issue', 3)).toBe(true);
  });

  it('does not cross kinds on the same number', () => {
    expect(isRowDeferred(deferred, 'issue', 7)).toBe(false);
    expect(isRowDeferred(deferred, 'pr', 3)).toBe(false);
  });

  it('returns false for undeferred rows and empty lists', () => {
    expect(isRowDeferred(deferred, 'pr', 12)).toBe(false);
    expect(isRowDeferred([], 'pr', 7)).toBe(false);
  });
});

describe('IssueDetails rendering and agent seeding', () => {
  const issueWithBody: IssueRow = {
    number: 101,
    title: 'Broken pipeline',
    state: 'open',
    assignees: [{ login: 'dev-alice' }],
    labels: [{ name: 'bug' }, { name: 'priority-high' }],
    updatedAt: '2026-04-01T12:00:00Z',
    body: 'Pipeline step 3 times out on Linux runners.',
  };

  const minimalIssue: IssueRow = {
    number: 102,
    title: 'Tidy comments',
  };

  it('renders title, state, assignees, labels, and body', () => {
    const html = renderToStaticMarkup(
      <IssueDetails repo="test-repo" issue={issueWithBody} onAskAgent={() => {}} />,
    );
    expect(html).toContain('data-testid="review-issue-details"');
    expect(html).toContain('#101 Broken pipeline');
    expect(html).toContain('open');
    expect(html).toContain('assigned: dev-alice');
    expect(html).toContain('labels: bug, priority-high');
    expect(html).toContain('updated 2026-04-01T12:00:00Z');
    expect(html).toContain('Pipeline step 3 times out on Linux runners.');
    expect(html).toContain('data-testid="ask-agent-issue-101"');
  });

  it('renders fallback when no description is present', () => {
    const html = renderToStaticMarkup(
      <IssueDetails repo="test-repo" issue={minimalIssue} onAskAgent={() => {}} />,
    );
    expect(html).toContain('#102 Tidy comments');
    expect(html).toContain('No description provided.');
  });
});
