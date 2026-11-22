// @vitest-environment happy-dom
// Regression guard for terminal/takeover.html (injected into ttyd's index.html
// by terminal/Dockerfile). #1389's input lock trapped phone Enter and was
// reverted, so this page must stay a plain button: no key handling, no focus
// games, no overlay. The script is executed in happy-dom with fetch, location
// and timers stubbed. happy-dom has no layout, so geometry is asserted on the
// CSS text and DOM structure, not on elementFromPoint.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const html = readFileSync(resolve(process.cwd(), '../terminal/takeover.html'), 'utf8');
const css = /<style>([\s\S]*?)<\/style>/.exec(html)?.[1] ?? '';
const script = /<script>([\s\S]*?)<\/script>/.exec(html)?.[1] ?? '';
const markup = html.replace(/<script>[\s\S]*?<\/script>/, '').replace(/<style>[\s\S]*?<\/style>/, '');

const KEY_EVENTS = ['keydown', 'keypress', 'keyup', 'beforeinput', 'input', 'paste', 'compositionstart', 'compositionend'];
const BLOCKED_ATTRS = ['contenteditable', 'readonly', 'inert'];

type Calls = { listeners: string[]; prevent: number; stop: number; focus: number; blur: number; attrs: string[] };
let calls: Calls;
let fetchMock: ReturnType<typeof vi.fn>;
let reload: ReturnType<typeof vi.fn>;
let restore: Array<() => void>;

function spy<T extends object, K extends keyof T>(obj: T, key: K, fn: (...a: never[]) => unknown) {
  const orig = obj[key];
  Object.defineProperty(obj, key, { configurable: true, writable: true, value: function (this: unknown, ...a: never[]) {
    fn(...a);
    return (orig as unknown as (...x: unknown[]) => unknown).apply(this, a);
  } });
  restore.push(() => Object.defineProperty(obj, key, { configurable: true, writable: true, value: orig }));
}

function load() {
  document.body.innerHTML = markup;
  new Function('window', 'document', 'fetch', 'location', script)(window, document, fetchMock, { reload });
}

const btn = () => document.getElementById('takeover-btn') as HTMLButtonElement;
const overlay = (text: string) => {
  const el = document.createElement('div');
  el.className = 'xterm-overlay';
  el.textContent = text;
  document.body.appendChild(el);
  return el;
};

beforeEach(() => {
  vi.useFakeTimers();
  calls = { listeners: [], prevent: 0, stop: 0, focus: 0, blur: 0, attrs: [] };
  restore = [];
  fetchMock = vi.fn();
  reload = vi.fn();
  spy(EventTarget.prototype, 'addEventListener', ((type: string) => { calls.listeners.push(type); }) as never);
  spy(Event.prototype, 'preventDefault', (() => { calls.prevent++; }) as never);
  spy(Event.prototype, 'stopPropagation', (() => { calls.stop++; }) as never);
  spy(HTMLElement.prototype, 'focus', (() => { calls.focus++; }) as never);
  spy(HTMLElement.prototype, 'blur', (() => { calls.blur++; }) as never);
  spy(Element.prototype, 'setAttribute', ((name: string) => { calls.attrs.push(name.toLowerCase()); }) as never);
  load();
});

afterEach(() => {
  restore.forEach((r) => r());
  vi.useRealTimers();
  document.body.innerHTML = '';
});

describe('takeover page leaves keyboard input alone', () => {
  it('registers no key, input or paste listeners and has no inline handlers besides the button click', () => {
    for (const t of KEY_EVENTS) expect(script).not.toMatch(new RegExp(`['"]${t}['"]`));
    expect(html).not.toMatch(/\son(?!click=)[a-z]+=/i);
    expect(calls.listeners.filter((t) => KEY_EVENTS.includes(t))).toEqual([]);
  });

  it('never prevents, stops, focuses or blurs, and only the button may be disabled', async () => {
    expect(script).not.toMatch(/preventDefault|stopPropagation|\.focus\(|\.blur\(|contenteditable|contentEditable|readOnly|readonly|inert/);
    fetchMock.mockResolvedValue({ ok: true, json: async () => ({}) });
    overlay('Connection Closed');
    await window.__switchTerminal();
    vi.advanceTimersByTime(5000);
    expect(calls.prevent).toBe(0);
    expect(calls.stop).toBe(0);
    expect(calls.focus).toBe(0);
    expect(calls.blur).toBe(0);
    expect(calls.attrs.filter((a) => BLOCKED_ATTRS.includes(a))).toEqual([]);
    expect(document.querySelectorAll('[contenteditable], [readonly], [inert]')).toHaveLength(0);
    expect(Array.from(document.querySelectorAll(':disabled')).every((e) => e.id === 'takeover-btn')).toBe(true);
  });
});

describe('takeover page adds no overlay', () => {
  it('adds only #takeover-bar with one button, and does not create elements at runtime', () => {
    const added = Array.from(document.body.children).filter((e) => e.tagName !== 'STYLE');
    expect(added.map((e) => e.id)).toEqual(['takeover-bar']);
    expect(added[0].querySelectorAll('*')).toHaveLength(1);
    expect(btn().tagName).toBe('BUTTON');
    expect(script).not.toMatch(/createElement|appendChild|insertAdjacent|innerHTML|\.append\(|\.prepend\(/);
  });

  it('keeps the bar small, top-right, and never full-screen or bottom-anchored', () => {
    const bar = /#takeover-bar\s*\{([^}]*)\}/.exec(css)?.[1] ?? '';
    expect(bar).toMatch(/top:\s*10px/);
    expect(bar).toMatch(/right:\s*10px/);
    expect(css).not.toMatch(/bottom\s*:/);
    expect(css).not.toMatch(/(width|height)\s*:\s*(100%|100v[wh]|100dvh)/);
    expect(css).not.toMatch(/inset\s*:/);
    expect(css).not.toMatch(/\bleft\s*:/);
  });

  it('after connect (no .xterm-overlay) the widget does not cover the middle of the page', () => {
    expect(document.querySelector('.xterm-overlay')).toBeNull();
    vi.advanceTimersByTime(3000);
    // happy-dom has no layout, so elementFromPoint cannot prove geometry; the
    // structural and CSS assertions above carry that proof.
    const bar = document.getElementById('takeover-bar')!;
    expect(bar.children).toHaveLength(1);
    expect(document.body.querySelectorAll('#takeover-bar, #takeover-bar *')).toHaveLength(2);
  });
});

describe('takeover poll only recolors the button', () => {
  it('does nothing when no overlay is present', () => {
    const before = btn().outerHTML;
    vi.advanceTimersByTime(5000);
    expect(btn().outerHTML).toBe(before);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(reload).not.toHaveBeenCalled();
  });

  it.each(['Connection Closed', 'Press ⏎ to Reconnect', 'Reconnecting'])('on overlay "%s" changes only style', (text) => {
    overlay(text);
    const label = btn().textContent;
    vi.advanceTimersByTime(1000);
    expect(btn().style.background).not.toBe('');
    expect(btn().disabled).toBe(false);
    expect(btn().textContent).toBe(label);
    expect(btn().getAttributeNames().sort()).toEqual(['class', 'id', 'onclick', 'style', 'type']);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(reload).not.toHaveBeenCalled();
    expect(calls.focus + calls.blur + calls.prevent).toBe(0);
  });

  it('ignores unrelated overlays', () => {
    overlay('80x24');
    vi.advanceTimersByTime(2000);
    expect(btn().style.background).toBe('');
  });
});

describe('takeover flow', () => {
  it('has the button wired to window.__switchTerminal', () => {
    expect(btn().getAttribute('onclick')).toBe('window.__switchTerminal()');
    expect(btn().textContent).toBe('Switch to this terminal');
  });

  it('POSTs the takeover intent once and reloads after success', async () => {
    fetchMock.mockResolvedValue({ ok: true, json: async () => ({}) });
    const p = window.__switchTerminal();
    expect(btn().disabled).toBe(true);
    await p;
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/buttons/api/terminal/takeover');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toEqual({ intent: 'terminal-takeover', confirmed: true });
    expect(reload).not.toHaveBeenCalled();
    vi.advanceTimersByTime(600);
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it('re-enables the button with a retry message and does not reload on a non-OK response', async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 500, json: async () => ({}) });
    await window.__switchTerminal();
    vi.advanceTimersByTime(5000);
    expect(btn().disabled).toBe(false);
    expect(btn().textContent).toMatch(/retry/);
    expect(reload).not.toHaveBeenCalled();
  });

  it('re-enables the button with a retry message and does not reload on a network error', async () => {
    fetchMock.mockRejectedValue(new Error('offline'));
    await window.__switchTerminal();
    vi.advanceTimersByTime(5000);
    expect(btn().disabled).toBe(false);
    expect(btn().textContent).toMatch(/retry/);
    expect(reload).not.toHaveBeenCalled();
  });
});

declare global {
  interface Window { __switchTerminal: () => Promise<void> }
}
