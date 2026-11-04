import { describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import CommandPalette, { groupedRows, isPaletteHotkey, movePaletteSelection } from './CommandPalette';
import {
  buildCommands,
  commandSection,
  filterCommands,
  fuzzyScore,
  inPrefixMode,
  parsePalettePrefix,
  type CommandContext,
  type PaletteCommand,
} from './commandRegistry';

function testCtx(overrides: Partial<CommandContext> = {}): CommandContext {
  return {
    go: vi.fn(),
    openSection: vi.fn(),
    reportBug: vi.fn(),
    focusChat: vi.fn(),
    refreshBriefing: vi.fn(),
    switchRepo: vi.fn(),
    openUrl: vi.fn(),
    terminalUrl: '/term',
    codeUrl: '/code',
    repos: [{ name: 'remote-server' }],
    repo: 'remote-server',
    ...overrides,
  };
}

function stubCommands(): PaletteCommand[] {
  const noop = () => {};
  return [
    { id: 'a', label: 'Go to Chat', hint: 'Navigate', run: noop },
    { id: 'b', label: 'Open Jobs section', hint: 'Now', run: noop },
  ];
}

describe('fuzzyScore', () => {
  it('matches everything on an empty query', () => {
    expect(fuzzyScore('', 'Go to Chat')).toBe(0);
    expect(fuzzyScore('   ', 'Go to Chat')).toBe(0);
  });

  it('matches case-insensitive subsequences', () => {
    expect(fuzzyScore('gtc', 'Go to Chat')).not.toBeNull();
    expect(fuzzyScore('CHAT', 'Go to Chat')).not.toBeNull();
    expect(fuzzyScore('xyz', 'Go to Chat')).toBeNull();
    expect(fuzzyScore('chat chat chat', 'Go to Chat')).toBeNull();
  });

  it('ranks word starts above consecutive runs above scatter', () => {
    const wordStart = fuzzyScore('ch', 'Go to Chat')!;
    const scattered = fuzzyScore('ch', 'Switch to bench')!;
    expect(wordStart).toBeGreaterThan(scattered);
    const consecutive = fuzzyScore('job', 'Open Jobs section')!;
    const spread = fuzzyScore('job', 'Adjust blobs')!;
    expect(consecutive).toBeGreaterThan(spread);
  });
});

describe('filterCommands', () => {
  it('returns the registry untouched on an empty query', () => {
    const cmds = stubCommands();
    expect(filterCommands(cmds, '')).toEqual(cmds);
    expect(filterCommands(cmds, '  ')).toEqual(cmds);
  });

  it('matches labels, hints, and keywords', () => {
    const cmds = buildCommands(testCtx());
    expect(filterCommands(cmds, 'briefing').map((c) => c.id)).toContain('go-briefing');
    expect(filterCommands(cmds, 'navigate').map((c) => c.id)).toContain('go-now');
    expect(filterCommands(cmds, 'merge').map((c) => c.id)).toContain('open-review');
    expect(filterCommands(cmds, 'zzz-no-such-command')).toEqual([]);
  });

  it('sorts best matches first', () => {
    const cmds = buildCommands(testCtx());
    const ids = filterCommands(cmds, 'jobs').map((c) => c.id);
    expect(ids[0]).toBe('open-jobs');
  });
});

describe('buildCommands', () => {
  it('lists 10+ working actions across views', () => {
    const cmds = buildCommands(testCtx());
    expect(cmds.length).toBeGreaterThanOrEqual(10);
    const hints = new Set(cmds.map((c) => c.hint));
    for (const group of ['Navigate', 'Now', 'Chat', 'Briefing', 'Tools']) {
      expect(hints.has(group)).toBe(true);
    }
    const ids = cmds.map((c) => c.id);
    for (const id of [
      'go-now',
      'go-chat',
      'go-briefing',
      'go-history',
      'open-review',
      'open-jobs',
      'report-bug',
      'focus-chat',
      'refresh-briefing',
      'open-terminal',
      'open-code',
    ]) {
      expect(ids).toContain(id);
    }
  });

  it('appends a switch command per other repo, skipping the current one', () => {
    const cmds = buildCommands(
      testCtx({ repos: [{ name: 'a' }, { name: 'b' }], repo: 'a' }),
    );
    const ids = cmds.map((c) => c.id);
    expect(ids).toContain('switch-repo-b');
    expect(ids).not.toContain('switch-repo-a');
  });

  it('runs call through to the wired context', () => {
    const ctx = testCtx({ repos: [{ name: 'a' }, { name: 'b' }], repo: 'a' });
    const byId = Object.fromEntries(buildCommands(ctx).map((c) => [c.id, c]));
    byId['go-history'].run();
    expect(ctx.go).toHaveBeenCalledWith('history');
    byId['open-review'].run();
    expect(ctx.openSection).toHaveBeenCalledWith('review');
    byId['report-bug'].run();
    expect(ctx.reportBug).toHaveBeenCalled();
    byId['focus-chat'].run();
    expect(ctx.focusChat).toHaveBeenCalled();
    byId['refresh-briefing'].run();
    expect(ctx.refreshBriefing).toHaveBeenCalled();
    byId['open-terminal'].run();
    expect(ctx.openUrl).toHaveBeenCalledWith('/term');
    byId['switch-repo-b'].run();
    expect(ctx.switchRepo).toHaveBeenCalledWith('b');
  });
});

describe('movePaletteSelection', () => {
  it('wraps around both ends', () => {
    expect(movePaletteSelection(2, 1, 3)).toBe(0);
    expect(movePaletteSelection(0, -1, 3)).toBe(2);
    expect(movePaletteSelection(0, 1, 3)).toBe(1);
  });

  it('lands on first/last from nowhere, null without rows', () => {
    expect(movePaletteSelection(null, 1, 3)).toBe(0);
    expect(movePaletteSelection(null, -1, 3)).toBe(2);
    expect(movePaletteSelection(null, 1, 0)).toBeNull();
    expect(movePaletteSelection(0, 1, 0)).toBeNull();
  });
});

describe('isPaletteHotkey', () => {
  it('accepts Ctrl+K and Cmd+K only', () => {
    expect(isPaletteHotkey({ key: 'k', ctrlKey: true, metaKey: false })).toBe(true);
    expect(isPaletteHotkey({ key: 'K', ctrlKey: false, metaKey: true })).toBe(true);
    expect(isPaletteHotkey({ key: 'k', ctrlKey: false, metaKey: false })).toBe(false);
    expect(isPaletteHotkey({ key: 'j', ctrlKey: true, metaKey: false })).toBe(false);
    expect(isPaletteHotkey({ key: 'k', ctrlKey: true, metaKey: false, altKey: true })).toBe(false);
  });
});

describe('CommandPalette markup', () => {
  const noop = () => {};

  it('renders nothing while closed', () => {
    const html = renderToStaticMarkup(
      <CommandPalette open={false} commands={stubCommands()} onClose={noop} onRun={noop} />,
    );
    expect(html).toBe('');
  });

  it('renders a labelled dialog with the full registry and a selected first row', () => {
    const html = renderToStaticMarkup(
      <CommandPalette open={true} commands={stubCommands()} onClose={noop} onRun={noop} />,
    );
    expect(html).toContain('data-testid="command-palette"');
    expect(html).toContain('role="dialog"');
    expect(html).toContain('data-testid="palette-input"');
    expect(html).toContain('role="combobox"');
    expect(html).toContain('role="listbox"');
    expect(html).toContain('Go to Chat');
    expect(html).toContain('Open Jobs section');
    expect(html).toContain('Navigate');
    expect(html).toContain('aria-selected="true"');
    expect(html).toContain('aria-activedescendant="palette-item-a"');
    expect(html).toContain('Esc closes');
  });

  it('renders the empty state when the registry has no rows', () => {
    const html = renderToStaticMarkup(
      <CommandPalette open={true} commands={[]} onClose={noop} onRun={noop} />,
    );
    expect(html).toContain('data-testid="palette-empty"');
    expect(html).toContain('No matching commands.');
  });
});

describe('grouped palette sections (issue #839)', () => {
  const noop = () => {};

  it('maps hints to the four sections', () => {
    const section = (hint: string) => commandSection({ id: 'x', label: 'x', hint, run: noop });
    expect(section('Navigate')).toBe('Navigation');
    expect(section('Now')).toBe('Actions');
    expect(section('Chat')).toBe('Actions');
    expect(section('Briefing')).toBe('Actions');
    expect(section('Repo')).toBe('Repositories');
    expect(section('Tools')).toBe('System');
    expect(section('System')).toBe('System');
  });

  it('opens a header per contiguous section run, keeping item indexes', () => {
    const rows = groupedRows(stubCommands());
    expect(rows.map((r) => (r.kind === 'header' ? r.section : r.cmd.id))).toEqual([
      'Navigation',
      'a',
      'Actions',
      'b',
    ]);
    const items = rows.filter((r) => r.kind === 'item');
    expect(items.map((r) => (r.kind === 'item' ? r.index : -1))).toEqual([0, 1]);
  });

  it('renders section headers above their commands', () => {
    const html = renderToStaticMarkup(
      <CommandPalette open={true} commands={stubCommands()} onClose={noop} onRun={noop} />,
    );
    expect(html).toContain('data-testid="palette-section"');
    expect(html).toContain('Navigation');
    expect(html).toContain('Actions');
    expect(html).toContain('@ repos');
  });
});

describe('palette prefix sub-modes (issue #839)', () => {
  it('parses the leading sigil, defaulting to the full pool', () => {
    expect(parsePalettePrefix('>sync')).toEqual({ mode: 'actions', rest: 'sync' });
    expect(parsePalettePrefix('@boom')).toEqual({ mode: 'repos', rest: 'boom' });
    expect(parsePalettePrefix('#48')).toEqual({ mode: 'review', rest: '48' });
    expect(parsePalettePrefix('/tpl')).toEqual({ mode: 'chat', rest: 'tpl' });
    expect(parsePalettePrefix('plain')).toEqual({ mode: 'all', rest: 'plain' });
    expect(parsePalettePrefix('')).toEqual({ mode: 'all', rest: '' });
  });

  it('narrows the pool per mode before matching', () => {
    const cmds = buildCommands(testCtx());
    const actions = filterCommands(cmds, '>');
    expect(actions.length).toBeGreaterThan(0);
    expect(actions.every((c) => inPrefixMode(c, 'actions'))).toBe(true);
    expect(actions.map((c) => c.id)).not.toContain('go-now');
    const repos = filterCommands(cmds, '@');
    expect(repos.every((c) => c.hint === 'Repo')).toBe(true);
    const review = filterCommands(cmds, '#').map((c) => c.id);
    expect(review).toContain('open-review');
    expect(review).toContain('new-issue');
    expect(review).not.toContain('open-code');
    const chat = filterCommands(cmds, '/').map((c) => c.id);
    expect(chat).toContain('focus-chat');
    expect(chat).not.toContain('go-history');
  });

  it('fuzzy-matches the rest within the narrowed pool', () => {
    const cmds = buildCommands(testCtx());
    expect(filterCommands(cmds, '>sync').map((c) => c.id)[0]).toBe('sync-now');
    expect(filterCommands(cmds, '#merge').map((c) => c.id)).toContain('open-review');
    expect(filterCommands(cmds, 'zzz-no-such-command')).toEqual([]);
  });
});

describe('palette operator verbs (issue #839)', () => {
  it('registers sync, stats, and new-issue commands', () => {
    const cmds = buildCommands(testCtx());
    const ids = cmds.map((c) => c.id);
    expect(ids).toContain('sync-now');
    expect(ids).toContain('open-stats');
    expect(ids).toContain('new-issue');
  });

  it('runs call through to the wired context, tolerating missing verbs', () => {
    const ctx = testCtx({ syncNow: vi.fn(), openStats: vi.fn(), newIssue: vi.fn() });
    const byId = Object.fromEntries(buildCommands(ctx).map((c) => [c.id, c]));
    byId['sync-now'].run();
    expect(ctx.syncNow).toHaveBeenCalled();
    byId['open-stats'].run();
    expect(ctx.openStats).toHaveBeenCalled();
    byId['new-issue'].run();
    expect(ctx.openSection).toHaveBeenCalledWith('review');
    expect(ctx.newIssue).toHaveBeenCalled();
    // Older wirings without the optional verbs do not throw.
    const legacy = Object.fromEntries(buildCommands(testCtx()).map((c) => [c.id, c]));
    expect(() => legacy['sync-now'].run()).not.toThrow();
    expect(() => legacy['new-issue'].run()).not.toThrow();
  });
});
