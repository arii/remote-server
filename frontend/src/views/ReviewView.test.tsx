import { afterEach, describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import ReviewView, {
  IssueRowTitle,
  RowActionButton,
  applyOptimisticMerge,
  isRowDeferred,
  nextPrAfterMerge,
  openIssueUrl,
  revertOptimisticMerge,
  reviewColumnsClass,
  rowMatchesFilter,
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

describe('review list filter', () => {
  const pr = { number: 576, title: 'Collapsed rows carry outcome clauses' };
  const issue = {
    number: 571,
    title: 'Repair picker is unfiltered',
    labels: [{ name: 'audit' }, 'ux'],
  };

  it('matches numbers with or without a hash', () => {
    expect(rowMatchesFilter(pr, '576')).toBe(true);
    expect(rowMatchesFilter(pr, '#57')).toBe(true);
    expect(rowMatchesFilter(pr, '999')).toBe(false);
  });

  it('matches titles case-insensitively', () => {
    expect(rowMatchesFilter(pr, 'outcome')).toBe(true);
    expect(rowMatchesFilter(pr, 'COLLAPSED')).toBe(true);
    expect(rowMatchesFilter(issue, 'outcome')).toBe(false);
  });

  it('matches issue labels in either shape', () => {
    expect(rowMatchesFilter(issue, 'audit')).toBe(true);
    expect(rowMatchesFilter(issue, 'UX')).toBe(true);
    expect(rowMatchesFilter(pr, 'audit')).toBe(false);
  });

  it('shows everything on a blank query', () => {
    expect(rowMatchesFilter(pr, '')).toBe(true);
    expect(rowMatchesFilter(pr, '   ')).toBe(true);
    expect(rowMatchesFilter(issue, '')).toBe(true);
  });
});

describe('row action accessible names (issue #569)', () => {
  it('names Open/Defer/Restore with kind + number', () => {
    const html = renderToStaticMarkup(
      <>
        <RowActionButton action="Open" kind="pr" num={12} onClick={() => {}} />
        <RowActionButton action="Defer" kind="pr" num={12} onClick={() => {}} />
        <RowActionButton action="Defer" kind="issue" num={559} onClick={() => {}} />
        <RowActionButton action="Restore" kind="issue" num={488} onClick={() => {}} />
      </>,
    );
    expect(html).toContain('aria-label="Open PR #12"');
    expect(html).toContain('aria-label="Defer PR #12"');
    expect(html).toContain('aria-label="Defer issue #559"');
    expect(html).toContain('aria-label="Restore issue #488"');
  });

  it('keeps visible text as the bare verb', () => {
    const html = renderToStaticMarkup(
      <RowActionButton action="Defer" kind="issue" num={559} onClick={() => {}} />,
    );
    expect(html).toContain('>Defer<');
    expect(html).not.toContain('>Defer issue #559<');
  });

  it('gives every repeated row action a unique name', () => {
    const html = renderToStaticMarkup(
      <>
        {[559, 560].map((n) => (
          <RowActionButton key={n} action="Defer" kind="issue" num={n} onClick={() => {}} />
        ))}
        {[7, 8].map((n) => (
          <RowActionButton key={`pr-${n}`} action="Open" kind="pr" num={n} onClick={() => {}} />
        ))}
      </>,
    );
    const names = [...html.matchAll(/aria-label="([^"]+)"/g)].map((m) => m[1]);
    expect(names).toHaveLength(4);
    expect(new Set(names).size).toBe(4);
  });
});

describe('review columns layout (issue #570)', () => {
  it('keeps two columns when detail content exists', () => {
    expect(reviewColumnsClass(true)).toBe('columns');
  });

  it('goes single column when the right side holds no detail', () => {
    expect(reviewColumnsClass(false)).toBe('columns columns-single');
  });

  it('wires the loading state to two columns (details prompt shows)', () => {
    const html = renderToStaticMarkup(
      <ReviewView repo="r" onAskAgent={() => {}} onSelectPr={() => {}} />,
    );
    expect(html).toContain('class="columns"');
    expect(html).not.toContain('columns-single');
  });
});
