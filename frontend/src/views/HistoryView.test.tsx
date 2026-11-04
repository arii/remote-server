import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import HistoryView, { HistoryInspector, RowsList } from './HistoryView';
import type { HistoryRow } from './historyUtils';

describe('history search input (issue #608)', () => {
  it('renders a search box beside the Kind filter', () => {
    const html = renderToStaticMarkup(<HistoryView repo="r" health={null} />);
    expect(html).toContain('data-testid="history-search"');
    expect(html).toContain('data-testid="history-kind-filter"');
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
