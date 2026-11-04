import { describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import CommandPalette, { isPaletteHotkey, movePaletteSelection } from './CommandPalette';
import {
  buildCommands,
  filterCommands,
  fuzzyScore,
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
