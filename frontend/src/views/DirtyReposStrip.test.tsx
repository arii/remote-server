import { describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import DirtyReposStrip, {
  commitViaAgentSeed,
  dirtyRepos,
  primaryRepoAction,
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

  it('routes each row state to one primary action (#958)', () => {
    expect(primaryRepoAction(dirtyBehind)).toBe('agent');
    expect(primaryRepoAction(behindOnly)).toBe('sync');
    expect(primaryRepoAction({ ...behindOnly, behind: 0 })).toBe('diff');
    expect(primaryRepoAction(clean)).toBe('diff');
  });

  it('lists every dirty/behind repo with one primary action plus drill-down', () => {
    const html = renderToStaticMarkup(
      <DirtyReposStrip repos={[clean, dirtyBehind, behindOnly]} {...handlers} />,
    );
    expect(html).toContain('data-testid="dirty-repos-strip"');
    expect(html).toContain('Dirty Repos (2)');
    expect(html).toContain('data-testid="dirty-row-boomtick"');
    expect(html).toContain('data-testid="dirty-row-hrm"');
    expect(html).not.toContain('data-testid="dirty-row-remote-server"');
    expect(html).toContain('2 changed · behind 3');
    // Dirty row: agent primary, no pull button at all.
    expect(html).toContain('data-testid="dirty-agent-boomtick"');
    expect(html).not.toContain('data-testid="dirty-pull-boomtick"');
    // Behind + clean row: pull primary, no agent button.
    expect(html).toContain('data-testid="dirty-pull-hrm"');
  });

  it('wraps row buttons in the styled actions hook (issue #1069)', () => {
    const html = renderToStaticMarkup(
      <DirtyReposStrip repos={[dirtyBehind, behindOnly]} {...handlers} />,
    );
    // CSS touch-target and card-layout rules hook off .actions.
    expect(html).toContain('class="actions"');
    expect(html).not.toContain('data-testid="dirty-agent-hrm"');
    // Every row still drills into the diff (stash/sync live there).
    expect(html).toContain('data-testid="dirty-view-diff-boomtick"');
    expect(html).toContain('data-testid="dirty-view-diff-hrm"');
    // The old six-button chrome is gone.
    expect(html).not.toContain('dirty-stash-');
    expect(html).not.toContain('dirty-autostash-');
    expect(html).not.toContain('dirty-chat-');
    expect(html).not.toContain('Auto-Stash &amp; Pull');
    expect(html).not.toContain('Open in Chat');
  });
});
