import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import HistoryView, { RowsList } from './HistoryView';

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
    expect(plain).toContain('No history yet.');
  });
});
