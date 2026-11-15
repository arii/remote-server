// @vitest-environment happy-dom
// Nav memory (bug 3 of the 2026-11-02 functional review): the
// picked repo and open tab survive a reload. loadNavMemory/
// saveNavMemory are unit-tested here; the App boot-restore and the
// write-back effect are exercised for real against <App /> in
// mock mode (no network), which static markup cannot do — the
// write-back lives in an effect.
import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import App, { loadNavMemory, saveNavMemory } from './App';

const KEY = 'console.nav-memory';

function seedStore(value: unknown): void {
  localStorage.setItem(
    KEY,
    typeof value === 'string' ? value : JSON.stringify(value),
  );
}

let root: Root | null = null;
let host: HTMLDivElement | null = null;

function mount(ui: ReactElement) {
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => {
    root!.render(ui);
  });
}

beforeEach(() => {
  // Mock mode keeps App's mount effects off the network; the nav
  // memory key starts each test empty.
  window.history.replaceState(null, '', '/?mock=1');
  localStorage.clear();
});

afterEach(() => {
  act(() => {
    root?.unmount();
  });
  root = null;
  host?.remove();
  host = null;
});

describe('nav memory store', () => {
  it('round-trips repo and tab through the localStorage key', () => {
    saveNavMemory({ repo: 'boomtick', tab: 'history' });
    expect(localStorage.getItem(KEY)).toContain('boomtick');
    expect(loadNavMemory()).toEqual({ repo: 'boomtick', tab: 'history' });
  });

  it('treats corrupt, empty, and off-enum memory as none', () => {
    for (const bad of [
      '{not json',
      'null',
      '"string"',
      '[]',
      {},
      { repo: '', tab: 'now' },
      { repo: 'boomtick' },
      { repo: 'boomtick', tab: 'settings' },
      { repo: 'boomtick', tab: null },
    ]) {
      seedStore(bad);
      expect(loadNavMemory()).toBeNull();
    }
    localStorage.removeItem(KEY);
    expect(loadNavMemory()).toBeNull();
  });

  it('treats a thrown localStorage read as none (private mode)', () => {
    const original = Storage.prototype.getItem;
    Storage.prototype.getItem = () => {
      throw new Error('denied');
    };
    try {
      expect(loadNavMemory()).toBeNull();
    } finally {
      Storage.prototype.getItem = original;
    }
  });
});

describe('App boot restore (bug 3)', () => {
  it('seeds the picker and pressed tab from stored memory', () => {
    seedStore({ repo: 'boomtick', tab: 'history' });
    const html = renderToStaticMarkup(<App />);
    // The picker keeps the stored repo visible even though the
    // boot repos list does not know it (fallback option), and the
    // History nav entry lights instead of Chat.
    expect(html).toContain('value="boomtick"');
    expect(html).toContain('selected=""');
    expect(html).toContain('aria-pressed="true"'); // one pressed tab…
    expect(html).toMatch(/data-testid="nav-history"[^>]*aria-pressed="true"|aria-pressed="true"[^>]*data-testid="nav-history"/);
    expect(html).not.toMatch(/data-testid="nav-chat"[^>]*aria-pressed="true"/);
  });

  it('falls back to defaults when memory holds a retired tab (D5)', () => {
    // Briefing was a tab before D5; a phone with old memory fails
    // validation (tab off-enum) and boots on the chat-first home.
    seedStore({ repo: 'boomtick', tab: 'briefing' });
    const html = renderToStaticMarkup(<App />);
    expect(html).toContain('value="remote-server"');
    expect(html).toMatch(/data-testid="nav-chat"[^>]*aria-pressed="true"/);
  });

  it('defaults to remote-server on Chat with empty memory (D5 chat-first)', () => {
    const html = renderToStaticMarkup(<App />);
    expect(html).toContain('value="remote-server"');
    expect(html).toMatch(/data-testid="nav-chat"[^>]*aria-pressed="true"/);
  });
});

describe('App write-back (bug 3)', () => {
  it('persists a tab switch so the next boot restores it', () => {
    mount(<App />);
    const history = host!.querySelector('[data-testid="nav-history"]');
    expect(history).not.toBeNull();
    act(() => {
      history!.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });
    expect(loadNavMemory()).toEqual({ repo: 'remote-server', tab: 'history' });
  });

  it('restores on the next mount end to end', () => {
    seedStore({ repo: 'tech-dancer', tab: 'history' });
    mount(<App />);
    // The write-back effect fired on mount without changing values…
    expect(loadNavMemory()).toEqual({ repo: 'tech-dancer', tab: 'history' });
    // …and the keep-alive History view mounted (first visit).
    expect(host!.querySelector('[data-testid="keepalive-history"]')).not.toBeNull();
  });
});
