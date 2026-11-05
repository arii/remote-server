import { describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import DirtyReposStrip, {
  commitViaAgentSeed,
  dirtyRepos,
  repoLagLabel,
} from './DirtyReposStrip';
import type { RepoInfo } from '../reviewTypes';

const clean: RepoInfo = {
  name: 'remote-server',
  full: 'arii/remote-server',
  branch: 'main',
  clean: true,
  ahead: 0,
  behind: 0,
  status_count: 0,
};
const dirtyBehind: RepoInfo = {
  name: 'boomtick',
  full: 'arii/boomtick',
  branch: 'main',
  clean: false,
  ahead: 0,
  behind: 3,
  status_count: 2,
};
const behindOnly: RepoInfo = {
  name: 'hrm',
  full: 'arii/hrm',
  branch: 'feat',
  clean: true,
  ahead: 1,
  behind: 2,
  status_count: 0,
};

const handlers = {
  onViewDiff: vi.fn(),
  onAction: vi.fn(async () => ({
    ok: true,
    repo: 'boomtick',
    action: 'stash',
    clean: true,
    detail: 'changes stashed',
  })),
  onAskAgent: vi.fn(),
  onOpenChat: vi.fn(),
};

describe('dirtyRepos (issue #937)', () => {
  it('keeps dirty, behind, and ahead repos only', () => {
    expect(dirtyRepos([clean, dirtyBehind, behindOnly]).map((r) => r.name)).toEqual([
      'boomtick',
      'hrm',
    ]);
    expect(dirtyRepos([clean])).toEqual([]);
  });

  it('treats missing lag fields as zero', () => {
    const bare: RepoInfo = { name: 'x', full: 'arii/x', branch: 'main', clean: true };
    expect(dirtyRepos([bare])).toEqual([]);
    expect(repoLagLabel(bare)).toBe('clean');
  });
});

describe('repoLagLabel (issue #937)', () => {
  it('summarizes changed, behind, and ahead counts', () => {
    expect(repoLagLabel(dirtyBehind)).toBe('2 changed · behind 3');
    expect(repoLagLabel(behindOnly)).toBe('behind 2 · ahead 1');
    expect(repoLagLabel(clean)).toBe('clean');
  });

  it('falls back to dirty without a status count', () => {
    expect(
      repoLagLabel({ name: 'x', full: 'arii/x', branch: 'main', clean: false }),
    ).toBe('dirty');
  });
});

describe('commitViaAgentSeed (issue #937)', () => {
  it('names the row repo and asks for a commit and push', () => {
    const seed = commitViaAgentSeed('boomtick');
    expect(seed).toContain('boomtick');
    expect(seed).toContain('commit');
    expect(seed).toContain('push');
  });
});

describe('DirtyReposStrip (issue #937)', () => {
  it('renders nothing when every repo is clean and current', () => {
    const html = renderToStaticMarkup(
      <DirtyReposStrip repos={[clean]} {...handlers} />,
    );
    expect(html).toBe('');
  });

  it('lists every dirty/behind repo with all one-click actions', () => {
    const html = renderToStaticMarkup(
      <DirtyReposStrip repos={[clean, dirtyBehind, behindOnly]} {...handlers} />,
    );
    expect(html).toContain('data-testid="dirty-repos-strip"');
    expect(html).toContain('Dirty Repos (2)');
    expect(html).toContain('data-testid="dirty-row-boomtick"');
    expect(html).toContain('data-testid="dirty-row-hrm"');
    expect(html).not.toContain('data-testid="dirty-row-remote-server"');
    expect(html).toContain('2 changed · behind 3');
    for (const repo of ['boomtick', 'hrm']) {
      expect(html).toContain(`data-testid="dirty-view-diff-${repo}"`);
      expect(html).toContain(`data-testid="dirty-stash-${repo}"`);
      expect(html).toContain(`data-testid="dirty-autostash-${repo}"`);
      expect(html).toContain(`data-testid="dirty-agent-${repo}"`);
      expect(html).toContain(`data-testid="dirty-pull-${repo}"`);
      expect(html).toContain(`data-testid="dirty-chat-${repo}"`);
    }
    expect(html).toContain('View Diff');
    expect(html).toContain('Auto-Stash &amp; Pull');
    expect(html).toContain('Commit via Agent');
    expect(html).toContain('Pull --rebase');
    expect(html).toContain('Open in Chat');
  });

  it('disables Pull --rebase on dirty rows', () => {
    const html = renderToStaticMarkup(
      <DirtyReposStrip repos={[dirtyBehind]} {...handlers} />,
    );
    const pull = html.match(/<button[^>]*dirty-pull-boomtick[^>]*>/)?.[0] ?? '';
    expect(pull).toContain('disabled');
  });
});
