import { afterEach, describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import ReviewView, {
  BulkDeferBar,
  BulkRestoreBar,
  DependabotBatchBar,
  IssueRowLabels,
  IssueRowTitle,
  MergeBlockDetail,
  PrDetails,
  PrDiff,
  PrDiffBody,
  PrRowApprove,
  PrRowRequestChanges,
  PrRowReviewActions,
  RequestChangesForm,
  ReviewListEmpty,
  RiskSummary,
  RowActionButton,
  buildRiskSummary,
  dependabotBatchNotice,
  detailsRiskSummary,
  planDependabotBatch,
  prRiskLevel,
  prRiskSummary,
  REVIEW_REPLY_TEMPLATES,
  appendReplyTemplate,
  applyOptimisticMerge,
  applyReviewDecision,
  approveEach,
  bulkApproveFailureText,
  bulkApproveNotice,
  bulkApproveSkippedNote,
  bulkDeferNotice,
  planBulkApprove,
  bulkRestoreNotice,
  bulkRowKey,
  dropMergedPr,
  isRowDeferred,
  issueLabelNames,
  linkedIssueNumbers,
  linkedPrNumbers,
  nextPrAfterMerge,
  prTitleRefs,
  mergeBadge,
  openIssueUrl,
  parseBulkRowKey,
  parseBulkSelection,
  revertOptimisticMerge,
  reviewColumnsClass,
  compareReviewRows,
  parseReviewSort,
  rowMatchesFilter,
  sortReviewRows,
  toggleBulkSelectAll,
  toggleBulkSelected,
  validateRequestChangesBody,
} from './ReviewView';
import { approveBlockReason, approveMergeEligible, isDependabotPr, isOwnPr, mergeBlockReason } from '../reviewTypes';
import type { PrRow } from '../reviewTypes';

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

describe('merged row leaves the list (issue #472)', () => {
  function row(n: number) {
    return {
      number: n,
      title: `PR #${n}`,
      author: 'a',
      branch: 'b',
      isDraft: false,
      ciStatus: 'passing',
      ciLabel: 'passing',
      reviewDecision: 'APPROVED',
      mergeable: 'MERGEABLE',
      url: `https://example.invalid/${n}`,
    };
  }

  it('drops the merged row, keeping the rest in order', () => {
    expect(dropMergedPr([row(3), row(7), row(9)], 7)?.map((r) => r.number)).toEqual([3, 9]);
  });

  it('empties the list when the last open PR merges', () => {
    expect(dropMergedPr([row(7)], 7)).toEqual([]);
  });

  it('leaves the list alone when the number is absent', () => {
    const prs = [row(3), row(9)];
    expect(dropMergedPr(prs, 12)?.map((r) => r.number)).toEqual([3, 9]);
  });

  it('stays null when the list never loaded', () => {
    expect(dropMergedPr(null, 7)).toBeNull();
  });

  it('does not mutate the input list', () => {
    const prs = [row(3), row(7)];
    dropMergedPr(prs, 7);
    expect(prs.map((r) => r.number)).toEqual([3, 7]);
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
  it('names Details/Defer/Restore with kind + number', () => {
    const html = renderToStaticMarkup(
      <>
        <RowActionButton action="Details" kind="pr" num={12} onClick={() => {}} />
        <RowActionButton action="Defer" kind="pr" num={12} onClick={() => {}} />
        <RowActionButton action="Defer" kind="issue" num={559} onClick={() => {}} />
        <RowActionButton action="Restore" kind="issue" num={488} onClick={() => {}} />
      </>,
    );
    expect(html).toContain('aria-label="Details PR #12"');
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
          <RowActionButton key={`pr-${n}`} action="Details" kind="pr" num={n} onClick={() => {}} />
        ))}
      </>,
    );
    const names = [...html.matchAll(/aria-label="([^"]+)"/g)].map((m) => m[1]);
    expect(names).toHaveLength(4);
    expect(new Set(names).size).toBe(4);
  });
});

describe('review columns layout (issues #570, #599)', () => {
  it('keeps two columns when detail content exists', () => {
    expect(reviewColumnsClass(true)).toBe('columns');
  });

  it('goes single column when the right side holds no detail', () => {
    expect(reviewColumnsClass(false)).toBe('columns columns-single');
  });

  it('wires no-selection to a single column with the prompt above the lists (#599)', () => {
    const html = renderToStaticMarkup(
      <ReviewView repo="r" onAskAgent={() => {}} onSelectPr={() => {}} />,
    );
    expect(html).toContain('columns columns-single');
    expect(html).toContain('data-testid="review-details-empty"');
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

describe('canned reply chips (issue #658)', () => {
  it('renders one chip per template above the body field', () => {
    const html = renderToStaticMarkup(
      <RequestChangesForm n={7} busy={false} onSubmit={() => {}} onCancel={() => {}} />,
    );
    expect(html).toContain('data-testid="review-reply-chips-7"');
    expect(html).toContain('aria-label="Canned replies"');
    const chips = html.match(/data-testid="review-reply-chip"/g) ?? [];
    expect(chips).toHaveLength(REVIEW_REPLY_TEMPLATES.length);
    for (const t of REVIEW_REPLY_TEMPLATES) {
      expect(html).toContain(`aria-label="Insert canned reply: ${t.label}"`);
      expect(html).toContain(`>${t.label}<`);
    }
    expect(html.indexOf('review-reply-chips-7')).toBeLessThan(
      html.indexOf('request-changes-body-7'),
    );
  });

  it('inserts into a blank draft verbatim', () => {
    const text = REVIEW_REPLY_TEMPLATES[0]?.text ?? '';
    expect(appendReplyTemplate('', text)).toBe(text);
    expect(appendReplyTemplate('   ', text)).toBe(text);
  });

  it('composes multiple taps as separated paragraphs without double spaces', () => {
    const [a, b] = REVIEW_REPLY_TEMPLATES.map((t) => t.text);
    expect(appendReplyTemplate(`${a}  `, `  ${b} `)).toBe(`${a}\n\n${b}`);
    const three = REVIEW_REPLY_TEMPLATES.slice(0, 3).map((t) => t.text);
    const composed = three.reduce((draft, snippet) => appendReplyTemplate(draft, snippet), '');
    expect(composed).toBe(three.join('\n\n'));
    expect(composed).not.toContain('  ');
  });

  it('leaves the draft alone on a blank snippet', () => {
    expect(appendReplyTemplate('  Keep me.  ', '   ')).toBe('Keep me.');
    expect(appendReplyTemplate('', '')).toBe('');
  });

  it('refuses overflow appends via the existing validator message', () => {
    const text = REVIEW_REPLY_TEMPLATES[0]?.text ?? '';
    const next = appendReplyTemplate('x'.repeat(2000), text);
    expect(next.length).toBeGreaterThan(2000);
    expect(validateRequestChangesBody(next)).toBe('Keep feedback under 2000 characters.');
  });

  it('keeps every template short, even combined', () => {
    for (const t of REVIEW_REPLY_TEMPLATES) {
      expect(t.label.length).toBeLessThanOrEqual(16);
      expect(validateRequestChangesBody(t.text)).toBeNull();
    }
    expect(REVIEW_REPLY_TEMPLATES.length).toBeGreaterThanOrEqual(5);
    const all = REVIEW_REPLY_TEMPLATES.map((t) => t.text).join('\n\n');
    expect(validateRequestChangesBody(all)).toBeNull();
  });

  it('hides the chip row while no request-changes field is open', () => {
    const html = renderToStaticMarkup(
      <ReviewView repo="r" onAskAgent={() => {}} onSelectPr={() => {}} />,
    );
    expect(html).not.toContain('review-reply-chip');
    expect(html).not.toContain('request-changes-form-');
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

  it('parses a whole selection, dropping malformed keys (issue #591)', () => {
    expect(parseBulkSelection(new Set(['pr:7', 'nope', 'issue:3']))).toEqual([
      { kind: 'pr', number: 7 },
      { kind: 'issue', number: 3 },
    ]);
    expect(parseBulkSelection(new Set())).toEqual([]);
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

describe('bulk restore selection (issue #591)', () => {
  it('names the restored count in the notice', () => {
    expect(bulkRestoreNotice(1)).toBe('Restored 1 row.');
    expect(bulkRestoreNotice(11)).toBe('Restored 11 rows.');
  });

  it('renders the bulk bar with the count and one Restore-n action', () => {
    const html = renderToStaticMarkup(
      <BulkRestoreBar count={2} busy={false} onRestore={() => {}} />,
    );
    expect(html).toContain('data-testid="restore-bulk-bar"');
    expect(html).toContain('2 selected');
    expect(html).toContain('data-testid="restore-bulk-restore"');
    expect(html).toContain('>Restore 2<');
    expect(html).toContain('aria-label="Restore 2 selected rows"');
  });

  it('disables the bulk action while the request is in flight', () => {
    const html = renderToStaticMarkup(
      <BulkRestoreBar count={2} busy onRestore={() => {}} />,
    );
    expect(html).toContain('disabled');
    expect(html).toContain('>Restoring…<');
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

describe('explicit branch field (issue #708)', () => {
  it('seeds merge/approve branch from the repo default', () => {
    const html = renderToStaticMarkup(
      <ReviewView repo="r" defaultBranch="main" onAskAgent={() => {}} onSelectPr={() => {}} />,
    );
    expect(html).toContain('data-testid="review-branch"');
    expect(html).toContain('value="main"');
  });

  it('renders empty without a default', () => {
    const html = renderToStaticMarkup(
      <ReviewView repo="r" onAskAgent={() => {}} onSelectPr={() => {}} />,
    );
    expect(html).toContain('data-testid="review-branch"');
    expect(html).toContain('value=""');
  });
});

describe('review list sort (issue #817)', () => {
  const rows = [
    { number: 1, title: 'old fix', updatedAt: '2026-10-01T00:00:00Z' },
    { number: 2, title: 'new feat', updatedAt: '2026-11-04T00:00:00Z' },
    { number: 3, title: 'mid chore', updatedAt: '2026-10-15T00:00:00Z' },
  ];

  it('parses the select value, defaulting to API order', () => {
    expect(parseReviewSort('newest')).toBe('newest');
    expect(parseReviewSort('oldest')).toBe('oldest');
    expect(parseReviewSort('api')).toBe('api');
    expect(parseReviewSort('bogus')).toBe('api');
    expect(parseReviewSort('')).toBe('api');
  });

  it('leaves API order byte-identical by default', () => {
    expect(sortReviewRows(rows, 'api')).toEqual(rows);
  });

  it('reorders newest and oldest by updatedAt', () => {
    expect(sortReviewRows(rows, 'newest').map((r) => r.number)).toEqual([2, 3, 1]);
    expect(sortReviewRows(rows, 'oldest').map((r) => r.number)).toEqual([1, 3, 2]);
  });

  it('keeps rows without timestamps in stable API order, never faked', () => {
    const mixed = [
      { number: 7, title: 'pr without stamp' },
      { number: 8, title: 'stamped', updatedAt: '2026-11-01T00:00:00Z' },
      { number: 9, title: 'another unstamped' },
    ];
    // Unstamped rows compare equal to everything: stable order kept.
    expect(sortReviewRows(mixed, 'newest').map((r) => r.number)).toEqual([7, 8, 9]);
    expect(sortReviewRows(mixed, 'oldest').map((r) => r.number)).toEqual([7, 8, 9]);
    expect(compareReviewRows({ number: 1 }, { number: 2 }, 'newest')).toBe(0);
  });

  it('treats unparseable timestamps as missing', () => {
    expect(
      compareReviewRows(
        { updatedAt: 'not-a-date' },
        { updatedAt: '2026-11-01T00:00:00Z' },
        'newest',
      ),
    ).toBe(0);
  });

  it('composes with the text filter', () => {
    const filtered = rows.filter((r) => rowMatchesFilter(r, 'fix'));
    expect(sortReviewRows(filtered, 'newest').map((r) => r.number)).toEqual([1]);
    const all = rows.filter((r) => rowMatchesFilter(r, ''));
    expect(sortReviewRows(all, 'oldest').map((r) => r.number)).toEqual([1, 3, 2]);
  });

  it('keeps bulk-selection keys order-independent', () => {
    const keys = (rs: typeof rows) =>
      rs.map((r) => bulkRowKey('issue', r.number)).sort();
    expect(keys(sortReviewRows(rows, 'newest'))).toEqual(keys(sortReviewRows(rows, 'api')));
    expect(keys(sortReviewRows(rows, 'oldest'))).toEqual(keys(sortReviewRows(rows, 'api')));
  });

  it('renders the sort select next to the filter, defaulting to API order', () => {
    const html = renderToStaticMarkup(
      <ReviewView repo="r" onAskAgent={() => {}} onSelectPr={() => {}} />,
    );
    expect(html).toContain('data-testid="review-sort"');
    expect(html).toContain('data-testid="review-filter"');
    expect(html).toContain('Newest first');
    expect(html).toContain('Oldest first');
  });
});

describe('PR↔issue linkage (issue #839)', () => {
  function pr(overrides = {}) {
    return {
      number: 4849,
      title: 'Fix webhook retry',
      author: 'a',
      branch: 'fix/webhook',
      isDraft: false,
      ciStatus: '',
      ciLabel: '',
      reviewDecision: '',
      mergeable: '',
      url: '',
      ...overrides,
    };
  }
  function issue(number: number, title: string) {
    return { number, title };
  }

  it('reads #N references from titles and branches', () => {
    expect(prTitleRefs('Fix it (closes #4848)')).toEqual([4848]);
    expect(prTitleRefs('fix/4848-thing #12 and #12 again')).toEqual([12]);
    expect(prTitleRefs('no refs here')).toEqual([]);
  });

  it('links explicit refs present in the loaded issues, ignoring ghosts', () => {
    const issues = [issue(4848, 'Webhook retry broken')];
    expect(linkedIssueNumbers(pr({ title: 'Fix it (closes #4848)' }), issues)).toEqual([4848]);
    expect(linkedIssueNumbers(pr({ branch: 'fix/#4848' }), issues)).toEqual([4848]);
    expect(linkedIssueNumbers(pr({ title: 'Fix it (closes #9999)' }), issues)).toEqual([]);
  });

  it('links same-title rows across the issue/PR filing pattern', () => {
    const issues = [issue(4848, 'Webhook retry broken')];
    expect(linkedIssueNumbers(pr({ title: 'webhook retry broken!' }), issues)).toEqual([4848]);
    expect(linkedIssueNumbers(pr({ title: 'Unrelated work' }), issues)).toEqual([]);
  });

  it('reverses the link from the issue side in number order', () => {
    const iss = issue(4848, 'Webhook retry broken');
    const prs = [
      pr({ number: 4850, title: 'webhook retry broken' }),
      pr({ number: 4849, title: 'Fix it (closes #4848)' }),
      pr({ number: 1, title: 'Unrelated' }),
    ];
    expect(linkedPrNumbers(iss, prs)).toEqual([4849, 4850]);
  });
});

describe('merge-block decision context (issue #839 addendum)', () => {
  function details(over = {}) {
    return {
      repo: 'r',
      number: 4849,
      title: 't',
      branch: 'b',
      base: 'main',
      ci: 'passing',
      failing: [],
      merge: 'clean',
      conflicted: false,
      draft: false,
      url: 'https://example.invalid/pr/4849',
      files: [],
      stats: { total_files: 0, additions: 0, deletions: 0 },
      ai_prompts: [],
      ...over,
    };
  }

  it('renders nothing when the PR is mergeable', () => {
    const html = renderToStaticMarkup(
      <MergeBlockDetail details={details()} head={null} />,
    );
    expect(html).toBe('');
  });

  it('names failing checks, conflicts, and draft beside the head reason', () => {
    const html = renderToStaticMarkup(
      <MergeBlockDetail
        details={details({ ci: 'failing', failing: ['e2e', 'lint'], conflicted: true, draft: true })}
        head="Not mergeable: CI failing, merge dirty."
      />,
    );
    expect(html).toContain('data-testid="review-merge-block"');
    expect(html).toContain('Not mergeable: CI failing, merge dirty.');
    expect(html).toContain('data-testid="review-merge-failing"');
    expect(html).toContain('Failing checks: e2e, lint.');
    expect(html).toContain('Has merge conflicts');
    expect(html).toContain('Still a draft.');
  });

  it('explains ruleset-blocked-with-green as a likely untriggered required check', () => {
    const html = renderToStaticMarkup(
      <MergeBlockDetail
        details={details({ merge: 'blocked' })}
        head="Not mergeable: CI passing, merge blocked."
      />,
    );
    expect(html).toContain('data-testid="review-merge-missing"');
    expect(html).toContain('a required check probably never ran (path filters)');
    expect(html).toContain('ruleset or branch protection');
    expect(html).toContain('https://example.invalid/pr/4849');
  });

  it('tells a behind branch to update instead', () => {
    const html = renderToStaticMarkup(
      <MergeBlockDetail
        details={details({ merge: 'behind' })}
        head="Not mergeable: CI passing, merge behind."
      />,
    );
    expect(html).toContain('Branch is behind main — update the branch, then merge.');
    expect(html).not.toContain('review-merge-missing');
  });
});

describe('approve/merge gating for self-authored PRs (issue #848)', () => {
  const SELF_REASON = 'you authored this PR — GitHub forbids self-approval';
  const base = {
    number: 7,
    title: 't',
    author: 'octo',
    branch: 'b',
    isDraft: false,
    ciStatus: 'passing',
    ciLabel: 'passing',
    reviewDecision: '',
    mergeable: 'MERGEABLE',
    url: 'https://example.invalid/7',
  };

  it('merge gate fails open when the repo requires no reviews', () => {
    expect(mergeBlockReason({ ...base, reviewRequired: false })).toBeNull();
    expect(mergeBadge({ ...base, reviewRequired: false })).toBe('merge-clean');
  });

  it('merge gate stays strict without the signal (fixtures, older payloads)', () => {
    expect(mergeBlockReason(base)).toBe('review is unapproved');
    expect(mergeBlockReason({ ...base, reviewRequired: true })).toBe('review is unapproved');
  });

  it('merge gate still blocks explicit negative reviews without required reviews', () => {
    expect(
      mergeBlockReason({ ...base, reviewRequired: false, reviewDecision: 'CHANGES_REQUESTED' }),
    ).toBe('review is CHANGES_REQUESTED');
    expect(
      mergeBlockReason({ ...base, reviewRequired: false, reviewDecision: 'REVIEW_REQUIRED' }),
    ).toBe('review is REVIEW_REQUIRED');
  });

  it('merge gate still blocks drafts, CI, and conflicts without required reviews', () => {
    expect(mergeBlockReason({ ...base, reviewRequired: false, isDraft: true })).toBe('is a draft');
    expect(mergeBlockReason({ ...base, reviewRequired: false, ciStatus: 'failing' })).toBe(
      'CI is failing',
    );
    expect(mergeBlockReason({ ...base, reviewRequired: false, mergeable: 'CONFLICTING' })).toBe(
      'merge state is CONFLICTING',
    );
  });

  it('approve blocks self with a viewer match (case-insensitive)', () => {
    expect(approveBlockReason({ ...base }, 'octo')).toBe(SELF_REASON);
    expect(approveBlockReason({ ...base }, 'OCTO')).toBe(SELF_REASON);
    expect(approveBlockReason({ ...base }, 'someone-else')).toBeNull();
  });

  it('approve stays legacy without a viewer', () => {
    expect(approveBlockReason({ ...base })).toBeNull();
    expect(approveBlockReason({ ...base }, '')).toBeNull();
    expect(approveBlockReason({ ...base, author: '' }, 'octo')).toBeNull();
  });

  it('approve keeps draft/approved precedence over self', () => {
    expect(approveBlockReason({ ...base, isDraft: true }, 'octo')).toBe('is a draft');
    expect(approveBlockReason({ ...base, reviewDecision: 'APPROVED' }, 'octo')).toBe(
      'already approved',
    );
  });

  it('matches author to viewer only', () => {
    expect(isOwnPr({ ...base }, 'octo')).toBe(true);
    expect(isOwnPr({ ...base }, 'OCTO')).toBe(true);
    expect(isOwnPr({ ...base }, 'someone-else')).toBe(false);
    expect(isOwnPr({ ...base }, undefined)).toBe(false);
    expect(isOwnPr({ ...base }, '')).toBe(false);
    expect(isOwnPr({ ...base, author: '' }, 'octo')).toBe(false);
  });

  it('approve chip shows the inline reason for own PRs', () => {
    const html = renderToStaticMarkup(
      <PrRowApprove pr={{ ...base }} busy={false} onApprove={() => {}} viewer="octo" />,
    );
    expect(html).toContain('disabled');
    expect(html).toContain('data-testid="approve-self-note-7"');
    expect(html).toContain(SELF_REASON);
  });

  it('approve chip shows no inline note for reviewable PRs', () => {
    const html = renderToStaticMarkup(
      <PrRowApprove pr={{ ...base }} busy={false} onApprove={() => {}} viewer="someone-else" />,
    );
    expect(html).not.toContain('approve-self-note-7');
    expect(html).not.toContain('disabled');
  });
});

describe('bulk approve selected PRs (issue #890)', () => {
  const SELF_REASON = 'you authored this PR — GitHub forbids self-approval';
  function row(n: number, over: Partial<PrRow> = {}): PrRow {
    return {
      number: n,
      title: `PR #${n}`,
      author: 'teammate',
      branch: 'b',
      isDraft: false,
      ciStatus: 'failing',
      ciLabel: 'failing',
      reviewDecision: '',
      mergeable: 'UNKNOWN',
      url: `https://example.invalid/${n}`,
      ...over,
    };
  }
  const prs = [row(1), row(2), row(3), row(4, { author: 'octo' })];

  it('approves the eligible subset, excluding self-authored with the #852 reason', () => {
    const plan = planBulkApprove(prs, new Set(['pr:1', 'pr:2', 'pr:3', 'pr:4']), 'octo');
    expect(plan.eligible).toEqual([1, 2, 3]);
    expect(plan.skipped).toEqual([{ number: 4, reason: SELF_REASON }]);
    expect(plan.issueCount).toBe(0);
  });

  it('skips drafts, already-approved, unlisted, and issue rows — never silently', () => {
    const mixed = [row(1), row(2, { isDraft: true }), row(3, { reviewDecision: 'APPROVED' })];
    const plan = planBulkApprove(
      mixed,
      new Set(['pr:1', 'pr:2', 'pr:3', 'pr:99', 'issue:5', 'nope']),
      'octo',
    );
    expect(plan.eligible).toEqual([1]);
    expect(plan.skipped).toEqual([
      { number: 2, reason: 'is a draft' },
      { number: 3, reason: 'already approved' },
      { number: 99, reason: 'not in the current list' },
    ]);
    expect(plan.issueCount).toBe(1);
  });

  it('stays legacy without a viewer: self-authored stays eligible, backend 400 is the fallback', () => {
    // Same fail-open as #848: no viewer means the client cannot know
    // authorship, so the row stays eligible and the backend #852 gate
    // (backend/tests/test_pulls_issues.py) refuses any self-approval.
    const plan = planBulkApprove(prs, new Set(['pr:4']), undefined);
    expect(plan.eligible).toEqual([4]);
    expect(plan.skipped).toEqual([]);
  });

  it('a self-only selection yields zero eligible, so the bar disables Approve', () => {
    const plan = planBulkApprove(prs, new Set(['pr:4']), 'octo');
    expect(plan.eligible).toEqual([]);
    const html = renderToStaticMarkup(
      <BulkDeferBar
        count={1}
        busy={false}
        onDefer={() => {}}
        approveCount={plan.eligible.length}
        onApprove={() => {}}
        skippedNote={bulkApproveSkippedNote(plan)}
      />,
    );
    expect(html).toContain('data-testid="defer-bulk-approve"');
    expect(html).toContain('>Approve 0<');
    expect(html).toContain('title="No selected PR is approvable"');
    expect(html).toContain('data-testid="defer-bulk-approve-skipped"');
    expect(html).toContain(SELF_REASON);
  });

  it('stays quiet on a clean selection, groups skip counts otherwise', () => {
    expect(bulkApproveSkippedNote({ eligible: [1], skipped: [], issueCount: 0 })).toBeNull();
    expect(
      bulkApproveSkippedNote({
        eligible: [1],
        skipped: [
          { number: 2, reason: 'already approved' },
          { number: 3, reason: 'already approved' },
          { number: 4, reason: SELF_REASON },
        ],
        issueCount: 1,
      }),
    ).toBe(
      '4 skipped: already approved (2); ' +
        `${SELF_REASON} (1); issue rows are not approvable (1)`,
    );
  });

  it('names the batch result: single mirrors the row notice, else counts', () => {
    expect(bulkApproveNotice([7], 1)).toBe('Approved PR #7.');
    expect(bulkApproveNotice([1, 2, 3], 3)).toBe('Approved 3 PRs.');
    expect(bulkApproveNotice([1, 2], 3)).toBe('Approved 2 of 3 PRs.');
    expect(bulkApproveNotice([], 3)).toBe('');
  });

  it('joins per-PR errors for partial and total failures', () => {
    expect(
      bulkApproveFailureText([
        { number: 7, error: 'POST api/pr-action failed: 400' },
        { number: 9, error: 'boom' },
      ]),
    ).toBe('PR #7: POST api/pr-action failed: 400; PR #9: boom');
  });

  it('fanout approves every number, collecting outcomes without aborting', async () => {
    const seen: number[] = [];
    const out = await approveEach(async (n) => {
      seen.push(n);
    }, [1, 2, 3]);
    expect(seen).toEqual([1, 2, 3]);
    expect(out).toEqual({ approved: [1, 2, 3], failed: [] });
  });

  it('fanout keeps successes when one PR fails, with per-PR errors', async () => {
    const out = await approveEach(async (n) => {
      if (n === 2) throw new Error('POST api/pr-action failed: 400');
      if (n === 3) throw 'plain-string failure';
    }, [1, 2, 3]);
    expect(out.approved).toEqual([1]);
    expect(out.failed).toEqual([
      { number: 2, error: 'POST api/pr-action failed: 400' },
      { number: 3, error: 'plain-string failure' },
    ]);
  });

  it('patches the reviewed rows optimistically, leaving the rest alone', () => {
    expect(applyReviewDecision(null, [7], 'APPROVED')).toBeNull();
    const next = applyReviewDecision(prs, [1, 3], 'APPROVED');
    expect(next?.map((r) => r.reviewDecision)).toEqual(['APPROVED', '', 'APPROVED', '']);
    expect(prs[0].reviewDecision).toBe('');
  });

  it('renders Approve-n with the skip note next to Defer-n', () => {
    const plan = planBulkApprove(prs, new Set(['pr:1', 'pr:2', 'pr:3', 'pr:4']), 'octo');
    const html = renderToStaticMarkup(
      <BulkDeferBar
        count={4}
        busy={false}
        onDefer={() => {}}
        approveCount={plan.eligible.length}
        onApprove={() => {}}
        skippedNote={bulkApproveSkippedNote(plan)}
      />,
    );
    expect(html).toContain('4 selected');
    expect(html).toContain('>Defer 4<');
    expect(html).toContain('>Approve 3<');
    expect(html).toContain('aria-label="Approve 3 selected PRs"');
    expect(html).not.toContain('disabled');
    expect(html).toContain('1 skipped:');
    expect(html).toContain(SELF_REASON);
  });

  it('disables both bar actions while either request is in flight', () => {
    const approving = renderToStaticMarkup(
      <BulkDeferBar
        count={3}
        busy={false}
        onDefer={() => {}}
        approveCount={3}
        approveBusy
        onApprove={() => {}}
      />,
    );
    expect(approving).toContain('>Approving…<');
    expect(approving).toContain('>Defer 3<');
    expect(approving.match(/disabled/g)?.length).toBe(2);
    const deferring = renderToStaticMarkup(
      <BulkDeferBar
        count={3}
        busy
        onDefer={() => {}}
        approveCount={3}
        onApprove={() => {}}
      />,
    );
    expect(deferring).toContain('>Deferring…<');
    expect(deferring).toContain('>Approve 3<');
    expect(deferring.match(/disabled/g)?.length).toBe(2);
  });
});

describe('review actionable rows (issue #907)', () => {
  const details = {
    repo: 'demo',
    number: 7,
    title: 'Fix thing',
    branch: 'feat/x',
    base: 'main',
    ci: 'passing',
    failing: [],
    merge: 'clean',
    conflicted: false,
    draft: false,
    url: 'https://example.test/pr/7',
    files: [{ path: 'a.py', additions: 3, deletions: 1, changes: 4 }],
    stats: { total_files: 1, additions: 3, deletions: 1 },
    ai_prompts: [{ id: 'p1', label: 'Risk', prompt: 'name the top risk' }],
  };

  it('offers Ask per review prompt and copy per file', () => {
    const html = renderToStaticMarkup(
      <PrDetails
        repo="demo"
        details={details}
        mergeBlocked={null}
        mergeDisabled={false}
        onMerge={() => {}}
        onAskAgent={() => {}}
      />,
    );
    expect(html).toContain('data-testid="ask-prompt-p1"');
    expect(html).toContain('>Ask</button>');
    expect(html).toContain('data-testid="review-file-copy-0"');
    expect(html).toContain('Copy path');
  });

  it('offers clear-filter on filtered empties only', () => {
    const filtered = renderToStaticMarkup(
      <ReviewListEmpty
        testid="review-prs-empty"
        filtering
        emptyText="No open PRs."
        onClearFilter={() => {}}
      />,
    );
    expect(filtered).toContain('No matches for this filter.');
    expect(filtered).toContain('data-testid="review-prs-empty-clear"');
    const bare = renderToStaticMarkup(
      <ReviewListEmpty
        testid="review-prs-empty"
        filtering={false}
        emptyText="No open PRs."
        onClearFilter={() => {}}
      />,
    );
    expect(bare).toContain('No open PRs.');
    expect(bare).not.toContain('review-prs-empty-clear');
  });

  it('renders diff bodies with a copy action', () => {
    const html = renderToStaticMarkup(
      <PrDiffBody
        pr={7}
        diff={{ number: 7, diff: 'diff --git a/a b/a\n+x\n', truncated: false }}
      />,
    );
    expect(html).toContain('data-testid="review-diff-body-7"');
    expect(html).toContain('data-testid="review-diff-copy-7"');
    expect(html).toContain('Copy diff');
  });
});

describe('inline diff + risk summary on cards (issue #938)', () => {
  const basePr: PrRow = {
    number: 7,
    title: 'Fix thing',
    author: 'octo',
    branch: 'fix-thing',
    isDraft: false,
    ciStatus: 'passing',
    ciLabel: 'passing',
    reviewDecision: 'APPROVED',
    mergeable: 'MERGEABLE',
    url: 'https://example.test/pr/7',
    additions: 10,
    deletions: 2,
    changedFiles: 1,
  };

  it('routine PR gets the low-risk 2-line summary', () => {
    const { summary, level } = buildRiskSummary({
      title: 'Fix thing', branch: 'fix-thing', ciStatus: 'passing',
      mergeable: 'MERGEABLE', additions: 10, deletions: 2, changedFiles: 1,
    });
    expect(level).toBe('low');
    expect(summary.split('\n')).toHaveLength(2);
    expect(summary).toContain('1 file changed (+10/-2)');
    expect(summary).toContain('Low risk; 0 breaking changes detected; CI green');
    expect(prRiskSummary(basePr)).toBe(summary);
    expect(prRiskLevel(basePr)).toBe('low');
  });

  it('prefers the backend summary verbatim when present', () => {
    const pr = {
      ...basePr,
      riskSummary: 'Minor dependency bump · 2 files changed (+5/-5)\nLow risk; 0 breaking changes detected; CI green',
      riskLevel: 'low',
    };
    expect(prRiskSummary(pr)).toContain('Minor dependency bump');
    const html = renderToStaticMarkup(<RiskSummary pr={pr} />);
    expect(html).toContain('data-testid="review-risk-summary-7"');
    expect(html).toContain('data-risk-level="low"');
    expect(html).toContain('Minor dependency bump');
    expect(html).toContain('CI green');
  });

  it('falls back to the mirror for older payloads without stats', () => {
    const bare: PrRow = {
      number: 9, title: 'Fix', author: 'a', branch: 'b', isDraft: false,
      ciStatus: 'passing', ciLabel: 'passing', reviewDecision: '',
      mergeable: 'MERGEABLE', url: '',
    };
    expect(prRiskSummary(bare).split('\n')[0]).toBe('No file changes detected · +0/-0');
    expect(prRiskSummary(bare).split('\n')).toHaveLength(2);
  });

  it('flags draft, failing, large, and breaking as high', () => {
    expect(buildRiskSummary({ title: 'WIP', isDraft: true, ciStatus: 'pending', additions: 1, changedFiles: 1 }).level).toBe('high');
    expect(buildRiskSummary({ title: 'Fix', ciStatus: 'failing', additions: 1, changedFiles: 1 }).level).toBe('high');
    expect(buildRiskSummary({ title: 'Big', ciStatus: 'passing', mergeable: 'MERGEABLE', additions: 400, deletions: 200, changedFiles: 25 }).level).toBe('high');
    expect(buildRiskSummary({ title: 'feat!: drop API', ciStatus: 'passing', mergeable: 'MERGEABLE', additions: 5, deletions: 5, changedFiles: 2 }).summary).toContain('Breaking change flagged');
    expect(buildRiskSummary({ title: 'Fix', ciStatus: 'pending', additions: 5, changedFiles: 1 }).level).toBe('medium');
  });

  it('renders card and details diffs with distinct testids', () => {
    const card = renderToStaticMarkup(<PrDiff repo="demo" pr={7} variant="card" />);
    expect(card).toContain('data-testid="review-card-diff-7"');
    expect(card).toContain('Diff preview');
    expect(card).not.toContain('data-testid="review-diff-7"');
    const pane = renderToStaticMarkup(<PrDiff repo="demo" pr={7} />);
    expect(pane).toContain('data-testid="review-diff-7"');
    expect(pane).toContain('View diff');
    const body = renderToStaticMarkup(
      <PrDiffBody pr={7} idPrefix="review-card-diff" diff={{ number: 7, diff: '+x', truncated: false }} />,
    );
    expect(body).toContain('data-testid="review-card-diff-body-7"');
    expect(body).toContain('data-testid="review-card-diff-copy-7"');
  });

  it('details pane shows the risk summary', () => {
    const d = {
      repo: 'demo', number: 7, title: 'Fix thing', branch: 'feat/x', base: 'main',
      ci: 'passing', failing: [], merge: 'clean', conflicted: false, draft: false,
      url: 'https://example.test/pr/7',
      files: [{ path: 'a.py', additions: 3, deletions: 1, changes: 4 }],
      stats: { total_files: 1, additions: 3, deletions: 1 },
      ai_prompts: [],
    };
    expect(detailsRiskSummary(d).level).toBe('low');
    const html = renderToStaticMarkup(
      <PrDetails repo="demo" details={d} mergeBlocked={null} mergeDisabled={false} onMerge={() => {}} onAskAgent={() => {}} />,
    );
    expect(html).toContain('data-testid="review-details-risk"');
    expect(html).toContain('CI green');
    expect(html).toContain('data-testid="review-diff-7"');
  });
});

describe('approve and merge compound (issue #932)', () => {
  function pr(over: Partial<PrRow> = {}): PrRow {
    return {
      number: 7,
      title: 'build(deps): bump x',
      author: 'dependabot[bot]',
      branch: 'dependabot/x',
      isDraft: false,
      ciStatus: 'passing',
      ciLabel: 'passing',
      reviewDecision: '',
      mergeable: 'MERGEABLE',
      url: 'https://example.test/pr/7',
      ...over,
    };
  }

  it('detects dependabot authors', () => {
    expect(isDependabotPr(pr())).toBe(true);
    expect(isDependabotPr(pr({ author: 'dependabot' }))).toBe(true);
    expect(isDependabotPr(pr({ author: 'alice' }))).toBe(false);
  });

  it('offers the compound only when approval is the sole blocker', () => {
    expect(approveMergeEligible(pr(), 'alice')).toBe(true);
    expect(approveMergeEligible(pr({ reviewDecision: 'REVIEW_REQUIRED' }), 'alice')).toBe(true);
    expect(approveMergeEligible(pr({ reviewDecision: 'APPROVED' }), 'alice')).toBe(false);
    expect(approveMergeEligible(pr({ reviewDecision: 'CHANGES_REQUESTED' }), 'alice')).toBe(false);
    expect(approveMergeEligible(pr({ ciStatus: 'failing' }), 'alice')).toBe(false);
    expect(approveMergeEligible(pr({ mergeable: 'CONFLICTING' }), 'alice')).toBe(false);
    expect(approveMergeEligible(pr({ isDraft: true }), 'alice')).toBe(false);
    expect(approveMergeEligible(pr(), 'dependabot[bot]')).toBe(false);
    expect(approveMergeEligible(pr({ reviewRequired: false }), 'alice')).toBe(false);
  });

  it('substitutes the compound for the merge+approve pair when eligible', () => {
    const noop = () => {};
    const html = renderToStaticMarkup(
      <PrRowReviewActions
        pr={pr()}
        viewer="alice"
        merging={false}
        reviewing={false}
        onMerge={noop}
        onApprove={noop}
        onApproveMerge={noop}
      />,
    );
    expect(html).toContain('data-testid="approve-merge-pr-7"');
    expect(html).toContain('Approve &amp; Merge');
    expect(html).not.toContain('data-testid="merge-pr-7"');
    expect(html).not.toContain('data-testid="approve-pr-7"');
  });

  it('keeps separate buttons when ineligible', () => {
    const noop = () => {};
    const html = renderToStaticMarkup(
      <PrRowReviewActions
        pr={pr({ ciStatus: 'failing' })}
        viewer="alice"
        merging={false}
        reviewing={false}
        onMerge={noop}
        onApprove={noop}
        onApproveMerge={noop}
      />,
    );
    expect(html).not.toContain('approve-merge-pr-7');
    expect(html).toContain('merge-pr-7');
    expect(html).toContain('approve-pr-7');
  });

  it('plans batches from eligible dependabots only, minimum two', () => {
    const green = pr({ number: 7 });
    const green2 = pr({ number: 9 });
    const failing = pr({ number: 10, ciStatus: 'failing' });
    const human = pr({ number: 11, author: 'alice' });
    expect(planDependabotBatch([green2, green], 'alice')).toEqual([7, 9]);
    expect(planDependabotBatch([green], 'alice')).toEqual([]);
    expect(planDependabotBatch([green, failing, human], 'alice')).toEqual([]);
  });

  it('summarizes batch outcomes like the approve batch', () => {
    expect(dependabotBatchNotice([7], 1)).toBe('Approved and merged PR #7.');
    expect(dependabotBatchNotice([7, 9], 2)).toBe('Approved and merged 2 Dependabot PRs.');
    expect(dependabotBatchNotice([7], 2)).toBe('Approved and merged 1 of 2 Dependabot PRs.');
    expect(dependabotBatchNotice([], 2)).toBe('');
  });

  it('renders the batch bar with its count', () => {
    const html = renderToStaticMarkup(
      <DependabotBatchBar count={3} busy={false} onFire={() => {}} />,
    );
    expect(html).toContain('data-testid="batch-approve-merge-dependabot"');
    expect(html).toContain('Batch Approve &amp; Merge Dependabot (3)');
  });

  it('labels the details pane GitHub link explicitly', () => {
    const html = renderToStaticMarkup(
      <PrDetails
        repo="demo"
        details={{
          repo: 'demo',
          number: 7,
          title: 'Fix thing',
          branch: 'feat/x',
          base: 'main',
          ci: 'passing',
          failing: [],
          merge: 'clean',
          conflicted: false,
          draft: false,
          url: 'https://example.test/pr/7',
          files: [],
          stats: { total_files: 0, additions: 0, deletions: 0 },
          ai_prompts: [],
        }}
        mergeBlocked={null}
        mergeDisabled={false}
        onMerge={() => {}}
        onAskAgent={() => {}}
      />,
    );
    expect(html).toContain('data-testid="open-github-pr-7"');
    expect(html).toContain('Open on GitHub');
  });
});
