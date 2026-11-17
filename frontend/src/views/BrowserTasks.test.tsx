// @vitest-environment happy-dom
// @vitest-environment-options {"happyDOM":{"settings":{"enableIframePageLoading":true}}}
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '../apiClient';
import type { BrowserTask } from '../apiClient';
import BrowserView from './BrowserView';

vi.mock('../apiClient', async (original) => ({
  ...await original<typeof import('../apiClient')>(),
  fetchBrowserTasks: vi.fn(), createBrowserTask: vi.fn(), cancelBrowserTask: vi.fn(),
  fetchPairedBrowserState: vi.fn(), fetchBrowserActivity: vi.fn(), fetchBrowserNotes: vi.fn(),
}));
vi.mock('./AgentDrawer', () => ({ default: () => <div>Agent console</div> }));

const task: BrowserTask = {
  id: 'abc123def456', instructions: 'Inspect the page', target: 'paired', mode: 'check',
  status: 'done', captures: [{ url: 'https://example.com/', title: 'Example', observed_text: 'Masked content', landed_on_login: false, shot: 0 }],
  url: 'https://example.com/', title: 'Example', observed_text: 'Masked content', landed_on_login: false, summary: 'Verified the page.',
};
let root: Root;
let host: HTMLDivElement;

async function mount() {
  await act(async () => root.render(<BrowserView browserState="IDLE" currentStep={null} iframeSrc="about:blank" error={null} onResume={() => {}} />));
}
async function selectChrome() {
  await act(async () => {
    const select = host.querySelector<HTMLSelectElement>('[aria-label="Browser target"]')!;
    select.value = 'paired';
    select.dispatchEvent(new Event('change', { bubbles: true }));
  });
}
async function fill(selector: string, value: string) {
  await act(async () => {
    const input = host.querySelector<HTMLInputElement | HTMLTextAreaElement>(selector)!;
    const prototype = input.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, 'value')!.set!.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.mocked(api.fetchBrowserTasks).mockResolvedValue([]);
  vi.mocked(api.fetchPairedBrowserState).mockResolvedValue({ connected: true, url: null, title: null });
  vi.mocked(api.fetchBrowserActivity).mockResolvedValue([]);
  vi.mocked(api.fetchBrowserNotes).mockResolvedValue({ notes: '', truncated: false });
  host = document.createElement('div'); document.body.append(host); root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove(); vi.resetAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers();
});

describe('browser targets and tasks', () => {
  it('switches the Grid stream to My Chrome and shows the unpaired setup hint', async () => {
    vi.mocked(api.fetchPairedBrowserState).mockResolvedValue({ connected: false, url: null, title: null });
    await mount();
    expect(host.querySelector('[data-testid="cobrowse-frame"]')).not.toBeNull();
    await selectChrome();
    expect(api.fetchPairedBrowserState).toHaveBeenCalledOnce();
    expect(host.querySelector('[data-testid="cobrowse-frame"]')).toBeNull();
    expect(host.textContent).toContain('Not paired');
    expect(host.querySelector<HTMLAnchorElement>('a')!.href).toContain('buttons-pair/README.md');
    expect(host.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled).toBe(true);
  });

  it('runs an explicit Check task against the selected target', async () => {
    const queued = { ...task, status: 'queued' as const, captures: [] };
    vi.mocked(api.createBrowserTask).mockResolvedValue(queued);
    vi.mocked(api.fetchBrowserTasks).mockResolvedValueOnce([]).mockResolvedValue([queued]);
    await mount(); await selectChrome();
    await fill('.browser-tasks textarea', 'Inspect the page');
    await fill('.browser-tasks input', 'https://example.com/');
    expect(host.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled).toBe(false);
    await act(async () => { host.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); });
    expect(api.createBrowserTask).toHaveBeenCalledWith({ instructions: 'Inspect the page', target: 'paired', mode: 'check', start_url: 'https://example.com/' });
    expect(host.textContent).toContain('queued');
  });

  it('renders report fields, collapsed observations and the latest paired screenshot', async () => {
    vi.mocked(api.fetchBrowserTasks).mockResolvedValue([task]);
    await mount(); await selectChrome();
    expect(host.textContent).toContain('Paired');
    expect(host.querySelector<HTMLImageElement>('[alt="Latest My Chrome task screenshot"]')!.getAttribute('src')).toBe(api.browserTaskShotUrl(task.id, 0));
    const report = host.querySelector<HTMLDetailsElement>('.browser-task-report')!;
    expect(report.open).toBe(false);
    report.open = true;
    expect(report.textContent).toContain('URL: https://example.com/');
    expect(report.textContent).toContain('Title: Example');
    expect(report.textContent).toContain('landed_on_login: false');
    expect(report.textContent).toContain('Verified the page.');
    expect(report.querySelector<HTMLDetailsElement>('details')!.open).toBe(false);
    expect(report.querySelector('pre')!.textContent).toBe('Masked content');
    expect(report.querySelectorAll('img')).toHaveLength(1);
  });

  it('cancels a running task and stops polling its completed list', async () => {
    const running = { ...task, status: 'running' as const };
    const cancelled = { ...task, status: 'cancelled' as const };
    vi.mocked(api.fetchBrowserTasks).mockResolvedValueOnce([running]).mockResolvedValue([cancelled]);
    vi.mocked(api.cancelBrowserTask).mockResolvedValue(cancelled);
    await mount(); await selectChrome();
    const report = host.querySelector<HTMLDetailsElement>('.browser-task-report')!;
    report.open = true;
    await act(async () => { report.querySelector('button')!.click(); });
    expect(api.cancelBrowserTask).toHaveBeenCalledWith(task.id);
    const calls = vi.mocked(api.fetchBrowserTasks).mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    expect(api.fetchBrowserTasks).toHaveBeenCalledTimes(calls);
    expect(host.textContent).toContain('cancelled');
  });

  it('polls while running and schedules nothing when idle', async () => {
    vi.mocked(api.fetchBrowserTasks).mockResolvedValueOnce([{ ...task, status: 'running' }]).mockResolvedValue([task]);
    await mount(); await selectChrome();
    expect(api.fetchBrowserTasks).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(api.fetchBrowserTasks).toHaveBeenCalledTimes(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(20000); });
    expect(api.fetchBrowserTasks).toHaveBeenCalledTimes(2);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('shows API errors and leaves an empty idle list unpolled', async () => {
    vi.mocked(api.fetchBrowserTasks).mockRejectedValue(new Error('Tasks unavailable'));
    vi.mocked(api.fetchPairedBrowserState).mockRejectedValue(new Error('Pairing unavailable'));
    await mount(); await selectChrome();
    expect(host.textContent).toContain('Tasks unavailable');
    expect(host.textContent).toContain('Pairing unavailable');
    expect(host.textContent).toContain('No tasks yet.');
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    expect(api.fetchBrowserTasks).toHaveBeenCalledOnce();
  });

  it('surfaces run and cancel failures without losing the report', async () => {
    vi.mocked(api.fetchBrowserTasks).mockResolvedValue([{ ...task, status: 'running' }]);
    vi.mocked(api.createBrowserTask).mockRejectedValue(new Error('Run rejected'));
    vi.mocked(api.cancelBrowserTask).mockRejectedValue(new Error('Cancel rejected'));
    await mount(); await selectChrome(); await fill('.browser-tasks textarea', 'Inspect');
    await act(async () => { host.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); });
    expect(host.textContent).toContain('Run rejected');
    await act(async () => { host.querySelector<HTMLButtonElement>('.browser-task-report button')!.click(); });
    expect(host.textContent).toContain('Cancel rejected');
    expect(host.querySelector('.browser-task-report')).not.toBeNull();
  });
});
