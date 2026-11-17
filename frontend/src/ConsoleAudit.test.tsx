// @vitest-environment happy-dom
import { act, StrictMode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App';
import * as api from './apiClient';
import { loadInbox } from './mocks/inbox';
import { loadRepoDiff, loadRepos, loadSystemHealth } from './views/mockViews';

vi.mock('./apiClient', async (original) => ({
  ...await original<typeof import('./apiClient')>(),
  fetchInbox: vi.fn(),
  fetchRepos: vi.fn(),
  fetchSystemHealth: vi.fn(),
  fetchPrs: vi.fn().mockResolvedValue({ prs: [] }),
  fetchJobs: vi.fn().mockResolvedValue({ jobs: [] }),
  fetchLedger: vi.fn().mockResolvedValue({ entries: [] }),
  fetchDispatchQueue: vi.fn().mockResolvedValue({ items: [] }),
  fetchDeferred: vi.fn().mockResolvedValue({ deferred: [] }),
  fetchSnoozed: vi.fn().mockResolvedValue({ snoozed: [] }),
  fetchRepoDiff: vi.fn(),
  warmRepoMetrics: vi.fn(),
  warmViewCaches: vi.fn(),
  prefetchTabPayloads: vi.fn(),
}));
vi.mock('./views/TriageDrawer', () => ({
  default: ({ open, section, onClose, onAskAgent }: { open: boolean; section: string; onClose: () => void; onAskAgent: (s: string) => void }) =>
    open ? <section data-testid={`${section}-view`}>
      <button data-testid="triage-close" onClick={onClose}>Close</button>
      <button data-testid="triage-shortcut" onClick={() => onAskAgent('Investigate this PR')}>Ask agent</button>
    </section> : null,
}));
vi.mock('./views/RepoDiffModal', () => ({
  default: ({ onAskAgent }: { onAskAgent: (s: string) => void }) =>
    <button data-testid="diff-shortcut" onClick={() => onAskAgent('Review this diff')}>Ask agent</button>,
}));
vi.mock('./views/ChatView', async (original) => ({
  ...await original<typeof import('./views/ChatView')>(),
  default: ({ seed, sendSeed }: { seed: string | null; sendSeed: boolean }) =>
    <div data-testid="chat-handoff" data-send={sendSeed}>{seed}</div>,
}));

let root: Root;
let host: HTMLDivElement;
const click = async (id: string) => {
  await act(async () => { host.querySelector<HTMLButtonElement>(`[data-testid="${id}"]`)!.click(); });
};

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Unconfigured test endpoint')));
  localStorage.clear();
  sessionStorage.clear();
  HTMLElement.prototype.scrollIntoView = vi.fn();
  vi.mocked(api.fetchInbox).mockResolvedValue(loadInbox());
  vi.mocked(api.fetchRepos).mockResolvedValue({ repos: loadRepos() });
  vi.mocked(api.fetchSystemHealth).mockResolvedValue(loadSystemHealth());
  vi.mocked(api.fetchRepoDiff).mockImplementation(async (repo) => loadRepoDiff(repo));
  host = document.createElement('div');
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe('console audit routing (#1317)', () => {
  it('returns from Review to Chat with the 1 shortcut', async () => {
    await act(async () => root.render(<App />));
    await click('nav-review');
    expect(host.querySelector('[data-testid="review-view"]')).not.toBeNull();
    await act(async () => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: '1', bubbles: true }));
    });
    expect(host.querySelector('[data-testid="chat-handoff"]')).not.toBeNull();
    expect(host.querySelector('[data-testid="review-view"]')).toBeNull();
    expect(host.querySelector('[data-testid="nav-chat"]')!.getAttribute('aria-current')).toBe('page');
  });

  it('defaults to Chat despite a persistent tab and restores the session tab on reload', async () => {
    localStorage.setItem('console.nav-memory', JSON.stringify({ repo: 'remote-server', tab: 'now' }));
    await act(async () => root.render(<App />));
    expect(host.querySelector('[data-testid="chat-handoff"]')).not.toBeNull();
    await click('nav-now');
    expect(sessionStorage.getItem('console.nav-tab')).toBe('now');
    expect(JSON.parse(localStorage.getItem('console.nav-memory')!)).toEqual({ repo: 'remote-server' });
    await act(async () => root.unmount());
    root = createRoot(host);
    await act(async () => root.render(<App />));
    expect(host.querySelector('[data-testid="now-view"]')).not.toBeNull();
    await act(async () => root.unmount());
    sessionStorage.clear();
    root = createRoot(host);
    await act(async () => root.render(<App />));
    expect(host.querySelector('[data-testid="chat-handoff"]')).not.toBeNull();
  });

  it.each(['review', 'jobs', 'queue', 'briefing'])('shows only %s until Close returns to Now', async (section) => {
    await act(async () => root.render(<App />));
    await click('nav-now');
    await click(`nav-${section}`);
    expect(host.querySelector(`[data-testid="${section}-view"]`)).not.toBeNull();
    expect(host.querySelector('[data-testid="now-view"]')).toBeNull();
    expect(host.querySelector('[data-testid="chat-handoff"]')).toBeNull();
    expect(host.querySelectorAll('[aria-label="Views"] [aria-pressed="true"]')).toHaveLength(1);
    expect(host.querySelector(`[data-testid="nav-${section}"]`)!.getAttribute('aria-current')).toBe('page');
    await click('triage-close');
    expect(host.querySelector('[data-testid="now-view"]')).not.toBeNull();
    expect(host.querySelector('[data-testid="nav-now"]')!.getAttribute('aria-current')).toBe('page');
    expect(host.querySelector(`[data-testid="${section}-view"]`)).toBeNull();
  });

  it('keeps the saved All repos gate on the Chat home', async () => {
    localStorage.setItem('console.nav-memory', JSON.stringify({ repo: 'all', tab: 'now' }));
    await act(async () => root.render(<App />));
    expect(host.querySelector('[data-testid="chat-handoff"]')).toBeNull();
    expect(host.querySelector('[data-testid="nav-chat"]')!.getAttribute('aria-current')).toBe('page');
    expect(host.querySelector('[data-testid="rollup-gate"]')!.textContent).toContain('Chat needs one repo');
  });

  it.each(['chat', 'now', 'history'])('leaves a section through the palette for %s', async (tab) => {
    await act(async () => root.render(<App />));
    await click('nav-review');
    await click('palette-open');
    await act(async () => host.querySelector<HTMLButtonElement>(`[data-testid="palette-item-go-${tab}"] button`)!.click());
    expect(host.querySelector('[data-testid="review-view"]')).toBeNull();
    expect(host.querySelector(`[data-testid="nav-${tab}"]`)!.getAttribute('aria-current')).toBe('page');
    expect(host.querySelectorAll('[aria-label="Views"] [aria-current="page"]')).toHaveLength(1);
  });

  it('leaves a section when the palette opens bug reporting', async () => {
    await act(async () => root.render(<App />));
    await click('nav-review');
    await click('palette-open');
    await act(async () => host.querySelector<HTMLButtonElement>('[data-testid="palette-item-report-bug"] button')!.click());
    expect(host.querySelector('[data-testid="review-view"]')).toBeNull();
    expect(host.querySelector('[data-testid="report-dialog"]')).not.toBeNull();
  });

  it('orders Attention, Review, composer, then a collapsed system disclosure without selection UI', async () => {
    await act(async () => root.render(<App />));
    await click('nav-now');
    const order = ['now-attention', 'now-composer', 'now-system'].map((id) => host.innerHTML.indexOf(`data-testid="${id}"`));
    expect(order[0]).toBeGreaterThan(0);
    expect(order[1]).toBeGreaterThan(order[0]);
    expect(order[2]).toBeGreaterThan(order[1]);
    expect(host.innerHTML.indexOf('aria-label="Review summary"')).toBeGreaterThan(order[0]);
    expect(host.innerHTML.indexOf('aria-label="Review summary"')).toBeLessThan(order[1]);
    expect(host.querySelector<HTMLDetailsElement>('[data-testid="now-system"]')!.open).toBe(false);
    expect(host.querySelector('[data-testid="now-view"] input[type="checkbox"]')).toBeNull();
    expect(host.querySelector('[data-testid="bottombar"]')).toBeNull();
    expect(host.querySelector('[data-testid="report-bug"]')).toBeNull();
    expect(host.querySelector('[aria-label="Views"] [data-testid="nav-briefing"]')).not.toBeNull();
  });

  it('stages and focuses triage shortcuts on Now, then explicitly sends through Chat', async () => {
    await act(async () => root.render(<StrictMode><App /></StrictMode>));
    await click('nav-review');
    await click('triage-shortcut');
    expect(host.querySelector('[data-testid="triage-shortcut"]')).toBeNull();
    const prompt = host.querySelector<HTMLTextAreaElement>('#now-prompt')!;
    expect(prompt.value).toBe('Investigate this PR');
    expect(document.activeElement).toBe(prompt);
    expect(host.querySelector('[data-testid="chat-handoff"]')).toBeNull();
    await act(async () => host.querySelector<HTMLFormElement>('[data-testid="now-composer"]')!.requestSubmit());
    const handoff = host.querySelector('[data-testid="chat-handoff"]')!;
    expect(handoff.textContent).toBe('Investigate this PR');
    expect(handoff.getAttribute('data-send')).toBe('true');
  });

  it('closes the diff modal before staging its shortcut', async () => {
    await act(async () => root.render(<App />));
    await click('nav-now');
    const diff = host.querySelector<HTMLButtonElement>('[data-testid^="dirty-view-diff-"]')!;
    expect(diff).not.toBeNull();
    await act(async () => diff.click());
    await click('diff-shortcut');
    expect(host.querySelector('[data-testid="diff-shortcut"]')).toBeNull();
    expect(host.querySelector<HTMLTextAreaElement>('#now-prompt')!.value).toBe('Review this diff');
  });

  it('refocuses an already-mounted composer when the same shortcut is chosen again', async () => {
    await act(async () => root.render(<App />));
    for (let visit = 0; visit < 2; visit++) {
      host.querySelector<HTMLButtonElement>('[data-testid="nav-review"]')!.focus();
      await click('nav-review');
      await click('triage-shortcut');
      const prompt = host.querySelector<HTMLTextAreaElement>('#now-prompt')!;
      expect(prompt.value).toBe('Investigate this PR');
      expect(document.activeElement).toBe(prompt);
    }
  });

  it('blocks a populated prompt for All repos without handing it to Chat', async () => {
    await act(async () => root.render(<App />));
    await click('nav-now');
    await act(async () => {
      const prompt = host.querySelector<HTMLTextAreaElement>('#now-prompt')!;
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(prompt, 'Investigate');
      prompt.dispatchEvent(new Event('input', { bubbles: true }));
    });
    const form = host.querySelector<HTMLFormElement>('[data-testid="now-composer"]')!;
    expect(form.querySelector('button')!.disabled).toBe(false);
    await act(async () => {
      const picker = host.querySelector<HTMLSelectElement>('[data-testid="repo-picker"]')!;
      picker.value = 'all';
      picker.dispatchEvent(new Event('change', { bubbles: true }));
    });
    expect(host.querySelector<HTMLTextAreaElement>('#now-prompt')!.value).toBe('Investigate');
    expect(form.querySelector('button')!.disabled).toBe(true);
    expect(form.textContent).toContain('Choose a repository before sending.');
    await act(async () => form.requestSubmit());
    expect(host.querySelector('[data-testid="chat-handoff"]')).toBeNull();
  });

  it('does not send an empty Now prompt and explains the disabled control', async () => {
    await act(async () => root.render(<App />));
    await click('nav-now');
    const form = host.querySelector<HTMLFormElement>('[data-testid="now-composer"]')!;
    expect(form.querySelector('button')!.disabled).toBe(true);
    expect(form.textContent).toContain('Enter a prompt to send.');
    await act(async () => form.requestSubmit());
    expect(host.querySelector('[data-testid="chat-handoff"]')).toBeNull();
  });

  it('opens and closes bug reporting from the header', async () => {
    await act(async () => root.render(<App />));
    await click('report-bug-header');
    expect(host.querySelector('[data-testid="report-dialog"]')).not.toBeNull();
    await click('report-cancel');
    expect(host.querySelector('[data-testid="report-dialog"]')).toBeNull();
  });

  it('keeps the composer usable when the inbox request fails', async () => {
    vi.mocked(api.fetchInbox).mockRejectedValue(new Error('Inbox unavailable'));
    await act(async () => root.render(<App />));
    await click('nav-now');
    expect(host.textContent).toContain('Inbox unavailable');
    expect(host.querySelector('#now-prompt')).not.toBeNull();
  });
});
