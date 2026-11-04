import { afterEach, describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import ReviewView, {
  BulkDeferBar,
  IssueRowLabels,
  IssueRowTitle,
  PrRowApprove,
  PrRowRequestChanges,
  RequestChangesForm,
  RowActionButton,
  applyOptimisticMerge,
  bulkDeferNotice,
  bulkRowKey,
  isRowDeferred,
  issueLabelNames,
  nextPrAfterMerge,
  mergeBadge,
  openIssueUrl,
  parseBulkRowKey,
  revertOptimisticMerge,
  reviewColumnsClass,
  rowMatchesFilter,
  toggleBulkSelectAll,
  toggleBulkSelected,
  validateRequestChangesBody,
} from './ReviewView';
import { approveBlockReason } from '../reviewTypes';

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

describe('merge badge', () => {
  const clean = {
    number: 1,
    title: 't',
    author: 'a',
    branch: 'b',
    isDraft: false,
    ciStatus: 'passing',
    ciLabel: 'passing',
    reviewDecision: 'APPROVED',
    mergeable: 'MERGEABLE',
    url: 'https://example.invalid/1',
  };

  it('reads merge-clean when nothing blocks', () => {
    expect(mergeBadge(clean)).toBe('merge-clean');
  });

  it('names the first block reason', () => {
    expect(mergeBadge({ ...clean, isDraft: true })).toBe('Blocked: is a draft');
    expect(mergeBadge({ ...clean, reviewDecision: 'CHANGES_REQUESTED' })).toBe(
      'Blocked: review is CHANGES_REQUESTED',
    );
    expect(mergeBadge({ ...clean, ciStatus: 'failing' })).toBe('Blocked: CI is failing');
    expect(mergeBadge({ ...clean, mergeable: 'CONFLICTING' })).toBe(
      'Blocked: merge state is CONFLICTING',
    );
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

describe('one-tap review chips (issue #616)', () => {
  const pr = {
    number: 7,
    title: 't',
    author: 'a',
    branch: 'b',
    isDraft: false,
    ciStatus: 'failing',
    ciLabel: 'failing',
    reviewDecision: '',
    mergeable: 'CONFLICTING',
    url: 'https://example.invalid/7',
  };

  it('enables approve on unreviewed PRs regardless of CI/merge state', () => {
    expect(approveBlockReason(pr)).toBeNull();
    expect(approveBlockReason({ ...pr, reviewDecision: 'CHANGES_REQUESTED' })).toBeNull();
    expect(approveBlockReason({ ...pr, ciStatus: 'passing', mergeable: 'MERGEABLE' })).toBeNull();
  });

  it('blocks approve on drafts and already-approved PRs', () => {
    expect(approveBlockReason({ ...pr, isDraft: true })).toBe('is a draft');
    expect(approveBlockReason({ ...pr, reviewDecision: 'APPROVED' })).toBe('already approved');
  });

  it('renders the approve chip with kind + number in the name', () => {
    const html = renderToStaticMarkup(<PrRowApprove pr={pr} busy={false} onApprove={() => {}} />);
    expect(html).toContain('data-testid="approve-pr-7"');
    expect(html).toContain('aria-label="Approve PR #7"');
    expect(html).toContain('>Approve<');
    expect(html).not.toContain('disabled');
  });

  it('disables the approve chip when blocked or busy', () => {
    const draft = renderToStaticMarkup(
      <PrRowApprove pr={{ ...pr, isDraft: true }} busy={false} onApprove={() => {}} />,
    );
    expect(draft).toContain('disabled');
    expect(draft).toContain('title="is a draft"');
    const approved = renderToStaticMarkup(
      <PrRowApprove pr={{ ...pr, reviewDecision: 'APPROVED' }} busy={false} onApprove={() => {}} />,
    );
    expect(approved).toContain('disabled');
    const busy = renderToStaticMarkup(<PrRowApprove pr={pr} busy onApprove={() => {}} />);
    expect(busy).toContain('disabled');
  });

  it('renders the request-changes toggle with expanded state', () => {
    const html = renderToStaticMarkup(
      <PrRowRequestChanges pr={pr} busy={false} expanded={false} onToggle={() => {}} />,
    );
    expect(html).toContain('data-testid="request-changes-pr-7"');
    expect(html).toContain('aria-label="Request changes on PR #7"');
    expect(html).toContain('aria-expanded="false"');
    expect(html).toContain('>Request changes<');
  });

  it('renders the inline body field with send + cancel', () => {
    const html = renderToStaticMarkup(
      <RequestChangesForm n={7} busy={false} onSubmit={() => {}} onCancel={() => {}} />,
    );
    expect(html).toContain('data-testid="request-changes-form-7"');
    expect(html).toContain('data-testid="request-changes-body-7"');
    expect(html).toContain('maxLength="2000"');
    expect(html).toContain('data-testid="request-changes-submit-7"');
    expect(html).toContain('>Send review<');
    expect(html).toContain('data-testid="request-changes-cancel-7"');
  });

  it('shows a sending state while the review posts', () => {
    const html = renderToStaticMarkup(
      <RequestChangesForm n={7} busy onSubmit={() => {}} onCancel={() => {}} />,
    );
    expect(html).toContain('>Sending…<');
  });

  it('requires a non-blank body and caps length', () => {
    expect(validateRequestChangesBody('')).toBe('Review feedback is required.');
    expect(validateRequestChangesBody('   ')).toBe('Review feedback is required.');
    expect(validateRequestChangesBody('Fix the leak.')).toBeNull();
    expect(validateRequestChangesBody('x'.repeat(2000))).toBeNull();
    expect(validateRequestChangesBody('x'.repeat(2001))).toBe(
      'Keep feedback under 2000 characters.',
    );
  });
});

describe('bulk defer selection (issue #607)', () => {
  it('keys rows by kind and number without collisions', () => {
    expect(bulkRowKey('pr', 7)).toBe('pr:7');
    expect(bulkRowKey('issue', 7)).toBe('issue:7');
    expect(parseBulkRowKey('pr:7')).toEqual({ kind: 'pr', number: 7 });
    expect(parseBulkRowKey('issue:3')).toEqual({ kind: 'issue', number: 3 });
  });

  it('rejects malformed selection keys', () => {
    expect(parseBulkRowKey('banana:7')).toBeNull();
    expect(parseBulkRowKey('pr:0')).toBeNull();
    expect(parseBulkRowKey('pr:7:extra')).toBeNull();
    expect(parseBulkRowKey('nope')).toBeNull();
  });

  it('toggles one row without mutating the prior set', () => {
    const prev = new Set(['pr:7']);
    const added = toggleBulkSelected(prev, 'issue:3');
    expect([...added].sort()).toEqual(['issue:3', 'pr:7']);
    expect(prev.has('issue:3')).toBe(false);
    expect([...toggleBulkSelected(added, 'pr:7')]).toEqual(['issue:3']);
  });

  it('select-all selects the whole list, then deselects it', () => {
    const keys = ['pr:7', 'pr:9'];
    const all = toggleBulkSelectAll(new Set(['issue:3']), keys);
    expect([...all].sort()).toEqual(['issue:3', 'pr:7', 'pr:9']);
    const none = toggleBulkSelectAll(all, keys);
    expect([...none]).toEqual(['issue:3']);
  });

  it('select-all completes a partial selection', () => {
    const all = toggleBulkSelectAll(new Set(['pr:7']), ['pr:7', 'pr:9']);
    expect([...all].sort()).toEqual(['pr:7', 'pr:9']);
  });

  it('names the deferred count in the notice', () => {
    expect(bulkDeferNotice(1)).toBe('Deferred 1 row.');
    expect(bulkDeferNotice(11)).toBe('Deferred 11 rows.');
  });

  it('renders the bulk bar with the count and one Defer-n action', () => {
    const html = renderToStaticMarkup(
      <BulkDeferBar count={3} busy={false} onDefer={() => {}} />,
    );
    expect(html).toContain('data-testid="defer-bulk-bar"');
    expect(html).toContain('3 selected');
    expect(html).toContain('data-testid="defer-bulk-defer"');
    expect(html).toContain('>Defer 3<');
    expect(html).toContain('aria-label="Defer 3 selected rows"');
  });

  it('disables the bulk action while the request is in flight', () => {
    const html = renderToStaticMarkup(
      <BulkDeferBar count={3} busy onDefer={() => {}} />,
    );
    expect(html).toContain('disabled');
    expect(html).toContain('>Deferring…<');
  });
});

describe('issue label names', () => {
  it('reads both the gh object shape and bare strings', () => {
    expect(issueLabelNames([{ name: 'audit' }, 'ready'])).toEqual(['audit', 'ready']);
  });

  it('returns an empty list for missing or blank labels', () => {
    expect(issueLabelNames(undefined)).toEqual([]);
    expect(issueLabelNames([])).toEqual([]);
    expect(issueLabelNames([{ name: '' }])).toEqual([]);
  });
});

describe('issue row labels', () => {
  const noop = () => {};

  function renderLabels(
    issue: { number: number; title: string; labels?: Array<{ name: string } | string> },
    extra: {
      repoLabels?: string[];
      pending?: { op: 'add' | 'remove'; label: string } | null;
      error?: string | null;
      initialOpen?: boolean;
    } = {},
  ) {
    return renderToStaticMarkup(
      <IssueRowLabels
        issue={issue}
        repoLabels={extra.repoLabels ?? ['audit', 'ready']}
        pending={extra.pending ?? null}
        error={extra.error ?? null}
        initialOpen={extra.initialOpen ?? false}
        onEnsureSuggestions={noop}
        onAdd={noop}
        onRemove={noop}
        onRetry={noop}
      />,
    );
  }

  it('renders one-tap remove per label with kind + number names', () => {
    const html = renderLabels({ number: 7, title: 'T', labels: [{ name: 'audit' }, 'ready'] });
    expect(html).toContain('data-testid="issue-labels-7"');
    expect(html).toContain('aria-label="Remove label audit from issue #7"');
    expect(html).toContain('aria-label="Remove label ready from issue #7"');
    expect(html).toContain('aria-label="Edit labels on issue #7"');
    expect(html).not.toContain('no labels');
  });

  it('shows the empty state for unlabeled rows', () => {
    const html = renderLabels({ number: 7, title: 'T', labels: [] });
    expect(html).toContain('no labels');
    expect(html).toContain('data-testid="edit-labels-issue-7"');
    expect(html).not.toContain('label-editor-7');
  });

  it('opens the add form with repo-label suggestions', () => {
    const html = renderLabels({ number: 7, title: 'T', labels: [] }, { initialOpen: true });
    expect(html).toContain('data-testid="label-editor-7"');
    expect(html).toContain('aria-label="Label name for issue #7"');
    expect(html).toContain('aria-label="Add label to issue #7"');
    expect(html).toContain('data-testid="label-suggestions-7"');
    expect(html).toContain('<option value="audit"');
    expect(html).toContain('<option value="ready"');
  });

  it('surfaces errors inline with retry', () => {
    const html = renderLabels({ number: 7, title: 'T', labels: [] }, { error: 'boom' });
    expect(html).toContain('data-testid="labels-error-7"');
    expect(html).toContain('role="alert"');
    expect(html).toContain('boom');
    expect(html).toContain('aria-label="Retry label change on issue #7"');
  });

  it('disables actions while an op is pending', () => {
    const html = renderLabels(
      { number: 7, title: 'T', labels: [{ name: 'audit' }] },
      { initialOpen: true, pending: { op: 'remove', label: 'audit' } },
    );
    expect(html).toContain('disabled');
  });

  it('marks the add button while an add is in flight', () => {
    const html = renderLabels(
      { number: 7, title: 'T', labels: [] },
      { initialOpen: true, pending: { op: 'add', label: 'audit' } },
    );
    expect(html).toContain('>Saving…<');
  });
});
