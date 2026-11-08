// @vitest-environment happy-dom
import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import RepairSection from './RepairSection';
import { fetchIssues, fetchRepairStatus } from '../apiClient';

// Issues-fetch fallback attribution (audit fallback batch): a
// rejected fetchIssues must surface the shared error state, and a
// later success must clear it. renderToStaticMarkup cannot exercise
// this — the fetch runs in an effect, so these mount for real like
// Dialog.test.tsx. apiClient is mocked, never the component.
vi.mock('../apiClient', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../apiClient')>();
  return { ...mod, fetchIssues: vi.fn(), fetchRepairStatus: vi.fn() };
});

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

function rerender(ui: ReactElement) {
  act(() => {
    root!.render(ui);
  });
}

beforeEach(() => {
  vi.mocked(fetchRepairStatus).mockResolvedValue({
    status: 'idle',
    timestamp: null,
    log_buffer: '',
    issue: null,
    exit_code: null,
    repo: null,
  });
});

afterEach(() => {
  act(() => {
    root?.unmount();
  });
  root = null;
  host?.remove();
  host = null;
  vi.clearAllMocks();
});

describe('RepairSection issues-fetch error state (audit fallback batch)', () => {
  it('failure surfaces the shared error state', async () => {
    vi.mocked(fetchIssues).mockRejectedValueOnce(new Error('issues down'));
    mount(<RepairSection repo="repo-fail" />);
    await vi.waitFor(() => {
      const alert = host!.querySelector('[role="alert"]');
      expect(alert).not.toBeNull();
      expect(alert!.textContent).toContain('Repair failed: issues down');
    });
  });

  it('success clears the error state', async () => {
    vi.mocked(fetchIssues).mockRejectedValueOnce(new Error('stale boom'));
    mount(<RepairSection repo="repo-flaky" />);
    await vi.waitFor(() => {
      const alert = host!.querySelector('[role="alert"]');
      expect(alert).not.toBeNull();
      expect(alert!.textContent).toContain('Repair failed: stale boom');
    });
    vi.mocked(fetchIssues).mockResolvedValueOnce({ repo: 'repo-flaky-2', issues: [] });
    rerender(<RepairSection repo="repo-flaky-2" />);
    await vi.waitFor(() => {
      expect(host!.querySelector('[role="alert"]')).toBeNull();
    });
    expect(host!.textContent).toContain('No open issues to repair.');
  });
});
