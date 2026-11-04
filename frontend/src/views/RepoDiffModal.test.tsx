import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import RepoDiffModal from './RepoDiffModal';

const data = {
  statusShort: ' M a.py\n?? b.py\n',
  diffStat: ' a.py | 2 +-\n 1 file changed\n',
  diff: 'diff --git a/a.py b/a.py\n+x\n',
  truncated: false,
};

describe('repo diff modal (issue #663)', () => {
  it('renders status, stat, and full diff under details', () => {
    const html = renderToStaticMarkup(
      <RepoDiffModal repo="demo" branch="main" data={data} error={null} onClose={() => {}} />,
    );
    expect(html).toContain('data-testid="repo-diff-modal"');
    expect(html).toContain('demo');
    expect(html).toContain('main');
    expect(html).toContain('data-testid="repo-diff-status"');
    expect(html).toContain('M a.py');
    expect(html).toContain('data-testid="repo-diff-stat"');
    expect(html).toContain('<details');
    expect(html).toContain('Full diff');
    expect(html).toContain('data-testid="repo-diff-close"');
  });

  it('flags truncated diffs', () => {
    const html = renderToStaticMarkup(
      <RepoDiffModal
        repo="demo"
        branch="main"
        data={{ ...data, truncated: true }}
        error={null}
        onClose={() => {}}
      />,
    );
    expect(html).toContain('truncated');
  });

  it('shows loading and error states', () => {
    const loading = renderToStaticMarkup(
      <RepoDiffModal repo="demo" branch="main" data={null} error={null} onClose={() => {}} />,
    );
    expect(loading).toContain('Loading');
    const failed = renderToStaticMarkup(
      <RepoDiffModal repo="demo" branch="main" data={null} error="boom" onClose={() => {}} />,
    );
    expect(failed).toContain('boom');
  });
});
