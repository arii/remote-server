import { describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import RepoDiffModal, { splitDiffByFile } from './RepoDiffModal';

const data = {
  statusShort: ' M a.py\n?? b.py\n',
  statusTruncated: false,
  diffStat: ' a.py | 2 +-\n 1 file changed\n',
  diff: 'diff --git a/a.py b/a.py\n+x\n',
  truncated: false,
  files: [
    { path: 'a.py', status: 'M', staged: false, unstaged: true, additions: 1, deletions: 1 },
    { path: 'b.py', status: '??', staged: false, unstaged: false, additions: 0, deletions: 0 },
  ],
  filesTruncated: false,
};

const props = {
  repo: 'demo',
  branch: 'main',
  onAction: vi.fn(),
  onAskAgent: vi.fn(),
  onClose: () => {},
};

describe('repo diff modal (issue #663)', () => {
  it('renders status, stat, and full diff under details', () => {
    const html = renderToStaticMarkup(
      <RepoDiffModal {...props} data={data} error={null} />,
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
        {...props}
        data={{ ...data, truncated: true }}
        error={null}
      />,
    );
    expect(html).toContain('truncated');
  });

  it('shows loading and error states', () => {
    const loading = renderToStaticMarkup(
      <RepoDiffModal {...props} data={null} error={null} />,
    );
    expect(loading).toContain('Loading');
    const failed = renderToStaticMarkup(
      <RepoDiffModal {...props} data={null} error="boom" />,
    );
    expect(failed).toContain('boom');
  });
});

describe('repo diff modal actions (issue #927)', () => {
  it('renders the file list with counts and staged notes', () => {
    const html = renderToStaticMarkup(
      <RepoDiffModal {...props} data={data} error={null} />,
    );
    expect(html).toContain('data-testid="repo-diff-files"');
    expect(html).toContain('a.py');
    expect(html).toContain('+1/-1');
    expect(html).toContain('??');
    expect(html).toContain('b.py');
  });

  it('renders one collapsible diff per file', () => {
    const html = renderToStaticMarkup(
      <RepoDiffModal {...props} data={data} error={null} />,
    );
    expect(html).toContain('data-testid="repo-diff-per-file"');
    expect(html).toContain('data-testid="repo-diff-file"');
    expect(html).toContain('a.py');
  });

  it('renders all four one-click actions', () => {
    const html = renderToStaticMarkup(
      <RepoDiffModal {...props} data={data} error={null} />,
    );
    expect(html).toContain('data-testid="repo-diff-stash"');
    expect(html).toContain('Stash changes');
    expect(html).toContain('data-testid="repo-diff-discard"');
    expect(html).toContain('Discard untracked');
    expect(html).toContain('data-testid="repo-diff-agent"');
    expect(html).toContain('Commit');
    expect(html).toContain('data-testid="repo-diff-sync"');
    expect(html).toContain('origin/main');
  });

  it('flags truncated file lists', () => {
    const html = renderToStaticMarkup(
      <RepoDiffModal
        {...props}
        data={{ ...data, filesTruncated: true }}
        error={null}
      />,
    );
    expect(html).toContain('showing first');
  });
});

describe('splitDiffByFile', () => {
  it('splits multi-file blobs on diff headers', () => {
    const parts = splitDiffByFile(
      'diff --git a/a.py b/a.py\n+x\n\ndiff --git a/b.py b/b.py\n+y\n',
    );
    expect(parts.map((p) => p.path)).toEqual(['a.py', 'b.py']);
    expect(parts[0].body).toContain('+x');
    expect(parts[1].body).toContain('+y');
  });

  it('unquotes paths with spaces', () => {
    const parts = splitDiffByFile(
      'diff --git "a/my file.py" "b/my file.py"\n+z\n',
    );
    expect(parts.map((p) => p.path)).toEqual(['my file.py']);
  });

  it('returns no chunks for an empty blob', () => {
    expect(splitDiffByFile('')).toEqual([]);
  });
});
