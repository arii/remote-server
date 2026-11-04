import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import HistoryView, { FollowButton, RowsList } from './HistoryView';

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

  it('offers an opt-in follow toggle beside refresh, off by default (issue #814)', () => {
    const html = renderToStaticMarkup(<HistoryView repo="r" health={null} />);
    expect(html).toContain('data-testid="history-refresh"');
    expect(html).toContain('data-testid="history-follow"');
    expect(html).toContain('aria-pressed="false"');
    expect(html).toContain('>Follow<');
    // Kind/search keep their own state, so follow refreshes never reset them.
    expect(html).toContain('data-testid="history-kind-filter"');
    expect(html).toContain('data-testid="history-search"');
  });

  it('follow button names both states with pressed state (issue #814)', () => {
    const off = renderToStaticMarkup(<FollowButton follow={false} onToggle={() => {}} />);
    expect(off).toContain('>Follow<');
    expect(off).toContain('aria-pressed="false"');
    const on = renderToStaticMarkup(<FollowButton follow onToggle={() => {}} />);
    expect(on).toContain('>Following<');
    expect(on).toContain('aria-pressed="true"');
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
