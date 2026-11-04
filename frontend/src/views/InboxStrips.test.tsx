import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import InboxStrips, { StripsBulkBar } from './InboxStrips';
import type { InboxFeed } from '../apiClient';
import type { DeferredRow } from '../reviewTypes';

const feed: InboxFeed = {
  repo: 'r',
  updated_at: 1,
  attention: [],
  merge_ready: {
    count: 2,
    top: [
      { number: 12, title: 'Fix it' },
      { number: 13, title: 'Polish' },
    ],
  },
  runs: { active: null, recent: [] },
};

function renderStrips(f: InboxFeed = feed, deferred: DeferredRow[] = []): string {
  return renderToStaticMarkup(
    <InboxStrips
      feed={f}
      repo="r"
      deferred={deferred}
      onDeferredChange={() => {}}
      onOpenReview={() => {}}
      onOpenJobs={() => {}}
    />,
  );
}

describe('inbox strips bulk selection (issue #591)', () => {
  it('renders selectable rows for merge-ready PRs plus select-all', () => {
    const html = renderStrips();
    expect(html).toContain('data-testid="inbox-strips"');
    expect(html).toContain('data-testid="strips-row-check-pr-12"');
    expect(html).toContain('data-testid="strips-row-check-pr-13"');
    expect(html).toContain('#12 Fix it');
    expect(html).toContain('data-testid="strips-select-all"');
  });

  it('renders deferred rows as selectable restore candidates', () => {
    const html = renderStrips(feed, [{ kind: 'issue', number: 3 }]);
    expect(html).toContain('data-testid="strips-row-check-issue-3"');
    expect(html).toContain('#3');
  });

  it('hides the bulk bar while nothing is selected', () => {
    expect(renderStrips()).not.toContain('data-testid="strips-bulk-bar"');
  });

  it('hides deferred PRs from the merge-ready rows (Review parity)', () => {
    const html = renderStrips(feed, [{ kind: 'pr', number: 12 }]);
    // Title survives once in the nav button; the row lives only in
    // the deferred section.
    expect(html.split('#12 Fix it').length - 1).toBe(1);
    expect(html.split('strips-row-check-pr-12').length - 1).toBe(1);
    expect(html).toContain('data-testid="strips-row-pr-12"');
  });

  it('renders nothing when no strip has content', () => {
    const empty: InboxFeed = {
      ...feed,
      merge_ready: { count: 0, top: [] },
      runs: { active: null, recent: [] },
    };
    expect(renderStrips(empty)).toBe('');
  });

  it('renders the deferred section even with no other strips', () => {
    const empty: InboxFeed = {
      ...feed,
      merge_ready: { count: 0, top: [] },
      runs: { active: null, recent: [] },
    };
    const html = renderStrips(empty, [{ kind: 'pr', number: 7 }]);
    expect(html).toContain('data-testid="inbox-strips"');
    expect(html).toContain('strips-row-check-pr-7');
  });

  it('renders Defer n + Restore n with the live count', () => {
    const html = renderToStaticMarkup(
      <StripsBulkBar count={3} busyOp={null} onDefer={() => {}} onRestore={() => {}} />,
    );
    expect(html).toContain('data-testid="strips-bulk-bar"');
    expect(html).toContain('3 selected');
    expect(html).toContain('data-testid="strips-bulk-defer"');
    expect(html).toContain('>Defer 3<');
    expect(html).toContain('data-testid="strips-bulk-restore"');
    expect(html).toContain('>Restore 3<');
  });

  it('disables both actions while one request is in flight', () => {
    const html = renderToStaticMarkup(
      <StripsBulkBar count={2} busyOp="defer" onDefer={() => {}} onRestore={() => {}} />,
    );
    expect(html).toContain('>Deferring…<');
    expect(html).not.toContain('>Restoring…<');
    // Both buttons disabled: two disabled attributes.
    expect(html.split('disabled').length - 1).toBe(2);
  });
});
