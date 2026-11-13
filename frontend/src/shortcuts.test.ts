import { describe, expect, it } from 'vitest';
import {
  APP_SHORTCUTS,
  clickChatSend,
  matchAppShortcut,
  type KeyPress,
  type ShortcutState,
} from './shortcuts';

function press(over: Partial<KeyPress> = {}): KeyPress {
  return { key: '', ctrlKey: false, metaKey: false, altKey: false, shiftKey: false, ...over };
}

function state(over: Partial<ShortcutState> = {}): ShortcutState {
  return { typing: false, suspended: false, helpOpen: false, ...over };
}

describe('shortcut table', () => {
  it('covers every action with a display key and label', () => {
    expect(APP_SHORTCUTS.length).toBeGreaterThan(0);
    for (const s of APP_SHORTCUTS) {
      expect(s.key.length).toBeGreaterThan(0);
      expect(s.label.length).toBeGreaterThan(0);
    }
    const actions = new Set(APP_SHORTCUTS.map((s) => s.action));
    for (const a of ['now', 'chat', 'history', 'browser', 'send', 'help', 'help-close', 'palette']) {
      expect(actions.has(a as never)).toBe(true);
    }
  });

  it('uses each display key once', () => {
    const keys = APP_SHORTCUTS.map((s) => s.key);
    expect(new Set(keys).size).toBe(keys.length);
  });

  it('gives Ctrl+K to the palette alone (issue #902)', () => {
    const ctrlK = APP_SHORTCUTS.filter((s) => s.key === 'Ctrl+K');
    expect(ctrlK).toHaveLength(1);
    expect(ctrlK[0].action).toBe('palette');
    const helpKeys = APP_SHORTCUTS.filter((s) => s.action === 'help').map((s) => s.key);
    expect(helpKeys).toEqual(['?']);
  });
});

describe('matchAppShortcut', () => {
  it('maps digits to tabs in nav order (chat-first, D5)', () => {
    expect(matchAppShortcut(press({ key: '1' }), state())).toBe('chat');
    expect(matchAppShortcut(press({ key: '2' }), state())).toBe('now');
    expect(matchAppShortcut(press({ key: '3' }), state())).toBe('history');
    expect(matchAppShortcut(press({ key: '4' }), state())).toBe('browser');
  });

  it('opens help on ? only; Ctrl/Cmd+K opens the palette (issue #902)', () => {
    expect(matchAppShortcut(press({ key: '?', shiftKey: true }), state())).toBe('help');
    // One shortcut, one surface: the chord never returns 'help'.
    expect(matchAppShortcut(press({ key: 'k', ctrlKey: true }), state())).toBe('palette');
    expect(matchAppShortcut(press({ key: 'k', metaKey: true }), state())).toBe('palette');
  });

  it('rejects near-miss palette chords (issue #902)', () => {
    expect(matchAppShortcut(press({ key: 'k' }), state())).toBeNull();
    expect(matchAppShortcut(press({ key: 'j', ctrlKey: true }), state())).toBeNull();
    expect(matchAppShortcut(press({ key: 'k', ctrlKey: true, altKey: true }), state())).toBeNull();
    expect(matchAppShortcut(press({ key: 'K', metaKey: true }), state())).toBeNull();
  });

  it('sends on Ctrl/Cmd+Enter when free', () => {
    expect(matchAppShortcut(press({ key: 'Enter', ctrlKey: true }), state())).toBe('send');
    expect(matchAppShortcut(press({ key: 'Enter', metaKey: true }), state())).toBe('send');
  });

  it('closes help on Escape only while open', () => {
    expect(matchAppShortcut(press({ key: 'Escape' }), state({ helpOpen: true }))).toBe('help-close');
    expect(matchAppShortcut(press({ key: 'Escape' }), state())).toBeNull();
  });

  it('skips bare keys while typing', () => {
    const typing = state({ typing: true });
    expect(matchAppShortcut(press({ key: '1' }), typing)).toBeNull();
    expect(matchAppShortcut(press({ key: '?' }), typing)).toBeNull();
  });

  it('keeps Ctrl+K alive while typing, defers Ctrl+Enter to the focused control', () => {
    const typing = state({ typing: true });
    expect(matchAppShortcut(press({ key: 'k', ctrlKey: true }), typing)).toBe('palette');
    // The composer already sends on Ctrl+Enter; the app level must not
    // fire a second send.
    expect(matchAppShortcut(press({ key: 'Enter', ctrlKey: true }), typing)).toBeNull();
  });

  it('suspends tabs and send under a modal, keeps help and palette reachable', () => {
    const suspended = state({ suspended: true });
    expect(matchAppShortcut(press({ key: '1' }), suspended)).toBeNull();
    expect(matchAppShortcut(press({ key: 'Enter', ctrlKey: true }), suspended)).toBeNull();
    expect(matchAppShortcut(press({ key: '?' }), suspended)).toBe('help');
    expect(matchAppShortcut(press({ key: 'k', ctrlKey: true }), suspended)).toBe('palette');
  });

  it('never hijacks modified chords or devtools', () => {
    expect(matchAppShortcut(press({ key: '1', ctrlKey: true }), state())).toBeNull();
    expect(matchAppShortcut(press({ key: '1', metaKey: true }), state())).toBeNull();
    expect(matchAppShortcut(press({ key: '1', altKey: true }), state())).toBeNull();
    // Ctrl+Shift+K opens devtools in Firefox.
    expect(matchAppShortcut(press({ key: 'K', ctrlKey: true, shiftKey: true }), state())).toBeNull();
    expect(matchAppShortcut(press({ key: 'Enter' }), state())).toBeNull();
  });

  it('ignores unknown keys', () => {
    expect(matchAppShortcut(press({ key: 'j' }), state())).toBeNull();
    expect(matchAppShortcut(press({ key: 'x' }), state())).toBeNull();
    expect(matchAppShortcut(press({ key: 'F1' }), state())).toBeNull();
  });
});

describe('clickChatSend', () => {
  it('clicks an enabled send button', () => {
    let clicked = 0;
    const ok = clickChatSend({
      querySelector: (sel: string) => {
        expect(sel).toBe('[data-testid="chat-send"]');
        return { disabled: false, click: () => { clicked += 1; } };
      },
    });
    expect(ok).toBe(true);
    expect(clicked).toBe(1);
  });

  it('skips a disabled or missing button', () => {
    let clicked = 0;
    const noop = () => { clicked += 1; };
    expect(clickChatSend({ querySelector: () => ({ disabled: true, click: noop }) })).toBe(false);
    expect(clickChatSend({ querySelector: () => null })).toBe(false);
    expect(clicked).toBe(0);
  });

  it('no-ops without a document', () => {
    expect(clickChatSend(undefined)).toBe(false);
  });
});
