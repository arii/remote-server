import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import HistoryView, { HistoryFilterChip, HistoryInspector, MemberBlock, RowsList, RunOutput, isSkipRow, rowToneClass } from './HistoryView';
import { toHistoryRows } from './historyUtils';
import type { HistoryRow } from './historyUtils';

describe('history search input (issue #608)', () => {
  it('renders a search box beside the Kind filter', () => {
    const html = renderToStaticMarkup(<HistoryView repo="r" />);
    expect(html).toContain('data-testid="history-search"');
    expect(html).toContain('data-testid="history-kind-filter"');
  });

  it('accepts a deep-link search seed without crashing (issue #930)', () => {
    // The nonce effect only runs client-side; static markup just
    // proves the prop threads through to a normal first paint.
    const html = renderToStaticMarkup(
      <HistoryView repo="r" queryRequest={{ query: 'deploy', nonce: 1 }} />,
    );
    expect(html).toContain('data-testid="history-search"');
  });

  it('explains an empty result while filtering, keeps the default copy otherwise', () => {
    const filtered = renderToStaticMarkup(<RowsList rows={[]} filtering />);
    expect(filtered).toContain('data-testid="history-empty"');
    expect(filtered).toContain('No matching rows.');
    const plain = renderToStaticMarkup(<RowsList rows={[]} filtering={false} />);
    expect(plain).toContain('No history yet');
  });

  it('guides first-run with an explainer and refresh action (issue #809)', () => {
    const plain = renderToStaticMarkup(<RowsList rows={[]} filtering={false} onRefresh={() => {}} />);
    expect(plain).toContain('data-testid="history-empty"');
    expect(plain).toContain('Finished jobs and ledger events land here');
    expect(plain).toContain('data-testid="history-empty-refresh"');
    expect(plain).toContain('>Refresh<');
    const filtered = renderToStaticMarkup(<RowsList rows={[]} filtering />);
    expect(filtered).toContain('No matching rows.');
    expect(filtered).not.toContain('history-empty-refresh');
  });
});

describe('history filter chip (issue #931)', () => {
  it('stays hidden when the feed is unfiltered', () => {
    expect(
      renderToStaticMarkup(<HistoryFilterChip query="" facet="all" onClear={() => {}} />),
    ).toBe('');
    expect(
      renderToStaticMarkup(<HistoryFilterChip query="   " facet="all" onClear={() => {}} />),
    ).toBe('');
  });

  it('names the container query with a clear action', () => {
    const html = renderToStaticMarkup(
      <HistoryFilterChip query="buttons" facet="all" onClear={() => {}} />,
    );
    expect(html).toContain('data-testid="history-filter-chip"');
    expect(html).toContain('Filter:');
    expect(html).toContain('buttons');
    expect(html).toContain('data-testid="history-filter-clear"');
    expect(html).toContain('>Clear</button>');
  });

  it('names the facet alongside the query', () => {
    const html = renderToStaticMarkup(
      <HistoryFilterChip query="buttons" facet="errors" onClear={() => {}} />,
    );
    expect(html).toContain('buttons');
    expect(html).toContain('Errors');
    const facetOnly = renderToStaticMarkup(
      <HistoryFilterChip query="" facet="ci" onClear={() => {}} />,
    );
    expect(facetOnly).toContain('data-testid="history-filter-chip"');
    expect(facetOnly).toContain('CI');
  });
});

describe('history inspector (issue #839)', () => {
  function historyRow(): HistoryRow {
    return {
      key: 'run-x',
      kind: 'run',
      summary: 'check-notifications failed ×3',
      at: 'then',
      repeat: 3,
      recovery: 'retry when the service is reachable',
      tone: 'bad',
      detail: '',
      members: [{ at: 'then', detail: 'exit: 1\nerror: boom', runId: 'run-1' }],
    };
  }

  it('marks the selected feed row without breaking the list', () => {
    const rows = [historyRow()];
    const html = renderToStaticMarkup(
      <RowsList rows={rows} filtering={false} selectedKey="run-x" onSelect={() => {}} />,
    );
    expect(html).toContain('data-testid="history-incident"');
    expect(html).toContain('history-row selected');
    expect(html).toContain('aria-current="true"');
  });

  it('inspects the row with full logs and a re-run per run member', () => {
    const html = renderToStaticMarkup(
      <HistoryInspector
        row={historyRow()}
        canRerun
        rerunBusy={null}
        rerunNote={null}
        onRerun={() => {}}
      />,
    );
    expect(html).toContain('data-testid="history-inspector"');
    expect(html).toContain('check-notifications failed ×3');
    expect(html).toContain('exit: 1');
    expect(html).toContain('Recovery: retry when the service is reachable');
    expect(html).toContain('data-testid="history-rerun-run-1"');
    expect(html).toContain('>Re-run<');
  });

  it('explains an empty inspector and hides re-run without live runs', () => {
    const empty = renderToStaticMarkup(
      <HistoryInspector
        row={null}
        canRerun
        rerunBusy={null}
        rerunNote={null}
        onRerun={() => {}}
      />,
    );
    expect(empty).toContain('No row selected');
    const mock = renderToStaticMarkup(
      <HistoryInspector
        row={historyRow()}
        canRerun={false}
        rerunBusy={null}
        rerunNote={null}
        onRerun={() => {}}
      />,
    );
    expect(mock).not.toContain('history-rerun-');
  });

  it('reports re-run progress inline', () => {
    const busy = renderToStaticMarkup(
      <HistoryInspector
        row={historyRow()}
        canRerun
        rerunBusy="run-1"
        rerunNote={null}
        onRerun={() => {}}
      />,
    );
    expect(busy).toContain('Re-running…');
    expect(busy).toContain('disabled');
    const noted = renderToStaticMarkup(
      <HistoryInspector
        row={historyRow()}
        canRerun
        rerunBusy={null}
        rerunNote="Re-run started: run-2"
        onRerun={() => {}}
      />,
    );
    expect(noted).toContain('data-testid="history-rerun-note"');
    expect(noted).toContain('Re-run started: run-2');
  });
});

describe('history actionable rows (issue #907)', () => {
  it('clears search and filters from the filtered empty state', () => {
    const html = renderToStaticMarkup(
      <RowsList rows={[]} filtering onClearFilters={() => {}} />,
    );
    expect(html).toContain('data-testid="history-clear-filters"');
    expect(html).toContain('Clear search');
  });

  it('names run output loads per run', () => {
    const html = renderToStaticMarkup(<RunOutput runId="abc123" />);
    expect(html).toContain('data-testid="history-run-output-loading-abc123"');
    expect(html).toContain('Loading run output…');
  });

  it('offers copy on inspector entries', () => {
    const html = renderToStaticMarkup(
      <MemberBlock member={{ at: 'then', detail: 'boom x1' }} grouped={false} />,
    );
    expect(html).toContain('data-testid="history-group-copy"');
    expect(html).toContain('Copy entry');
    expect(html).toContain('boom x1');
  });
});

describe('ledger noise filter and inline status (issue #964)', () => {
  const skip: HistoryRow = {
    key: 'webhook-a', kind: 'webhook', summary: 'Skipped: tree not ff-able',
    at: 'now', repeat: 1, recovery: '', tone: 'ok', detail: 'd', members: [],
  };
  const failedSkip: HistoryRow = { ...skip, key: 'webhook-b', tone: 'bad' };
  const good: HistoryRow = { ...skip, key: 'job-a', kind: 'job', summary: 'deployed', tone: 'ok' };
  const muted: HistoryRow = { ...skip, key: 'chat-a', kind: 'chat', summary: 'Chat session activity', tone: 'muted' };

  it('flags non-failure skips, never failures', () => {
    expect(isSkipRow(skip)).toBe(true);
    expect(isSkipRow({ ...skip, tone: 'muted' })).toBe(true);
    expect(isSkipRow(failedSkip)).toBe(false);
    expect(isSkipRow(good)).toBe(false);
  });

  it('maps tones to edge classes, muted to none', () => {
    expect(rowToneClass('bad')).toBe(' tone-bad');
    expect(rowToneClass('ok')).toBe(' tone-ok');
    expect(rowToneClass('muted')).toBe('');
  });

  it('paints green/red edges on rows without expanding', () => {
    const html = renderToStaticMarkup(
      <RowsList rows={[failedSkip, good, muted]} filtering={false} />,
    );
    expect(html).toContain('class="history-row tone-bad"');
    expect(html).toContain('class="history-row tone-ok"');
    expect(html).toContain('class="history-row"');
    expect(html).not.toContain('history-row tone-muted');
  });

  it('hides skips behind an unchecked toggle by default', () => {
    const html = renderToStaticMarkup(<HistoryView repo="r" />);
    expect(html).toContain('data-testid="history-show-skips"');
    expect(html).not.toContain('data-testid="history-skips-note"');
  });

  it('folds identical consecutive skips into one expandable row', () => {
    const entry = (ts: number) => ({
      kind: 'webhook', repo: 'r', ok: true,
      detail: 'skipped: tree not ff-able', ts,
    });
    const folded = toHistoryRows([], [entry(3), entry(2)]);
    expect(folded).toHaveLength(1);
    expect(folded[0].repeat).toBe(2);
    expect(folded[0].members).toHaveLength(2);
    expect(isSkipRow(folded[0])).toBe(true);
    const html = renderToStaticMarkup(<RowsList rows={folded} filtering={false} />);
    expect(html).toContain('data-testid="history-incident"');
    expect(html).toContain('×2');
  });
});
