import { afterEach, describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import {
  IssueRowTitle,
  applyOptimisticMerge,
  isRowDeferred,
  nextPrAfterMerge,
  openIssueUrl,
  revertOptimisticMerge,
} from './ReviewView';

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

describe('issue row open action', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('links the title when the row carries a URL', () => {
    const html = renderToStaticMarkup(
      <IssueRowTitle issue={{ number: 7, title: 'Flaky CI', url: 'https://example.invalid/i/7' }} />,
    );
    expect(html).toContain('href="https://example.invalid/i/7"');
    expect(html).toContain('target="_blank"');
    expect(html).toContain('#7 Flaky CI');
  });

  it('renders plain text without a URL', () => {
    const html = renderToStaticMarkup(<IssueRowTitle issue={{ number: 7, title: 'Flaky CI' }} />);
    expect(html).not.toContain('<a');
    expect(html).toContain('#7 Flaky CI');
  });

  it('opens the URL in a new tab', () => {
    const open = vi.fn();
    vi.stubGlobal('window', { open });
    openIssueUrl('https://example.invalid/i/7');
    expect(open).toHaveBeenCalledWith('https://example.invalid/i/7', '_blank', 'noopener');
  });

  it('ignores missing URLs', () => {
    const open = vi.fn();
    vi.stubGlobal('window', { open });
    openIssueUrl(undefined);
    openIssueUrl('');
    expect(open).not.toHaveBeenCalled();
  });
});
