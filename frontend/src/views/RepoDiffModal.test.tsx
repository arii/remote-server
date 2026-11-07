import { describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import RepoDiffModal, {
  diffLineKind,
  fileStatusLabel,
  groupDiffFiles,
  nextChangeIndex,
  splitDiffByFile,
} from './RepoDiffModal';

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

describe('repo diff modal polish (issue #942)', () => {
  it('renders as a native modal dialog, no hand-rolled overlay', () => {
    const html = renderToStaticMarkup(<RepoDiffModal {...props} data={data} error={null} />);
    expect(html).toContain('<dialog');
    expect(html).toContain('role="dialog"');
    expect(html).toContain('aria-modal="true"');
    expect(html).not.toContain('repo-diff-overlay');
  });

  it('colors diff lines and keeps the full hunk header in its title', () => {
    const hunk = '@@ -1,2 +1,2 @@ export function orchestratorSession(see DiscussionThread)';
    const colored = {
      ...data,
      diff: [
        'diff --git a/a.py b/a.py',
        '--- a/a.py',
        '+++ b/a.py',
        hunk,
        ' context line',
        '+added line',
        '-removed line',
        '',
      ].join('\n'),
    };
    const html = renderToStaticMarkup(<RepoDiffModal {...props} data={colored} error={null} />);
    expect(html).toContain('diff-add');
    expect(html).toContain('diff-del');
    expect(html).toContain('diff-ctx');
    expect(html).toContain('diff-hunk');
    expect(html).toContain(`title="${hunk}"`);
  });

  it('groups the file list with human status labels', () => {
    const html = renderToStaticMarkup(<RepoDiffModal {...props} data={data} error={null} />);
    expect(html).toContain('data-testid="repo-diff-group-modified"');
    expect(html).toContain('data-testid="repo-diff-group-untracked"');
    expect(html).toContain('data-testid="repo-diff-file-jump"');
    expect(html).toContain('>Untracked<');
  });

  it('offers expand-all and collapse-all for per-file diffs', () => {
    const html = renderToStaticMarkup(<RepoDiffModal {...props} data={data} error={null} />);
    expect(html).toContain('data-testid="repo-diff-expand-all"');
    expect(html).toContain('data-testid="repo-diff-collapse-all"');
  });

  it('caps a long diff behind an explicit show-more', () => {
    const lines = ['diff --git a/a.py b/a.py', '@@ -0,0 +1,150 @@'];
    for (let i = 0; i < 150; i += 1) lines.push(`+line-${i}`);
    const long = { ...data, diff: lines.join('\n') };
    const html = renderToStaticMarkup(<RepoDiffModal {...props} data={long} error={null} />);
    expect(html).toContain('+line-0');
    expect(html).not.toContain('+line-140');
    expect(html).toContain('Show more');
    expect(html).toContain('more lines');
  });

  it('renders a filter input for the file list', () => {
    const html = renderToStaticMarkup(
      <RepoDiffModal {...props} data={data} error={null} />,
    );
    expect(html).toContain('data-testid="repo-diff-filter"');
    expect(html).toContain('for="repo-diff-filter"');
  });

  it('hides the jump wall for large unfiltered lists', () => {
    const files = Array.from({ length: 19 }, (_, i) => ({
      path: `f${i}.py`,
      status: 'M',
      staged: false,
      unstaged: true,
      additions: 1,
      deletions: 0,
    }));
    const html = renderToStaticMarkup(
      <RepoDiffModal
        {...props}
        data={{ ...data, files, diffStat: '19 files' }}
        error={null}
      />,
    );
    expect(html).not.toContain('data-testid="repo-diff-files"');
    expect(html).toContain('data-testid="repo-diff-per-file"');
    expect(html).toContain('Changed files (19');
  });

  it('keeps jump chips for short lists', () => {
    const html = renderToStaticMarkup(
      <RepoDiffModal {...props} data={data} error={null} />,
    );
    expect(html).toContain('data-testid="repo-diff-files"');
    expect(html).toContain('data-testid="repo-diff-group-modified"');
  });

  it('styles destructive actions as danger and names the agent flow', () => {
    const html = renderToStaticMarkup(<RepoDiffModal {...props} data={data} error={null} />);
    expect(html).toContain('btn-danger');
    expect(html).toContain('data-testid="repo-diff-discard"');
    expect(html).toContain('data-testid="repo-diff-sync"');
    expect(html).toContain('Chat agent');
    expect(html).toContain('commit message');
  });
});

describe('diffLineKind', () => {
  it('classifies unified-diff lines', () => {
    expect(diffLineKind('+added')).toBe('add');
    expect(diffLineKind('-removed')).toBe('del');
    expect(diffLineKind('@@ -1 +1 @@ fn')).toBe('hunk');
    expect(diffLineKind('diff --git a/x b/x')).toBe('meta');
    expect(diffLineKind('+++ b/x')).toBe('meta');
    expect(diffLineKind('--- a/x')).toBe('meta');
    expect(diffLineKind(' context')).toBe('ctx');
  });
});

describe('file status helpers', () => {
  it('labels porcelain codes for humans', () => {
    expect(fileStatusLabel({ status: '??' })).toBe('Untracked');
    expect(fileStatusLabel({ status: 'M' })).toBe('Modified');
    expect(fileStatusLabel({ status: 'A' })).toBe('Added');
    expect(fileStatusLabel({ status: 'D' })).toBe('Deleted');
  });

  it('groups modified before untracked, dropping empty groups', () => {
    const groups = groupDiffFiles(data.files);
    expect(groups.map((g) => g.key)).toEqual(['modified', 'untracked']);
    expect(groups[0].files.map((f) => f.path)).toEqual(['a.py']);
    expect(groups[1].files.map((f) => f.path)).toEqual(['b.py']);
    expect(groupDiffFiles([data.files[0]]).map((g) => g.key)).toEqual(['modified']);
  });

  it('cycles change positions with wraparound', () => {
    expect(nextChangeIndex([2, 5, 9], -1)).toBe(2);
    expect(nextChangeIndex([2, 5, 9], 2)).toBe(5);
    expect(nextChangeIndex([2, 5, 9], 9)).toBe(2);
    expect(nextChangeIndex([], 0)).toBe(-1);
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
