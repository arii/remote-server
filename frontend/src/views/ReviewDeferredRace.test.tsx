// @vitest-environment happy-dom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, expect, it, vi } from 'vitest';
import ReviewView from './ReviewView';
import { deferRow, deferRowsBulk, fetchDeferred, restoreRow, startAuthedInterval } from '../apiClient';
vi.mock('../apiClient', async (original) => ({
  ...await original<typeof import('../apiClient')>(),
  fetchPrs: vi.fn().mockResolvedValue({ prs: [7, 8].map(number => ({
    number, title: `PR ${number}`, author: 'other', branch: 'feature',
    isDraft: false, ciStatus: 'failing', ciLabel: 'failing', reviewDecision: '', mergeable: 'CONFLICTING',
  })) }),
  fetchIssues: vi.fn().mockImplementation(async (repo: string) => ({ issues: [
    { number: 7, title: `${repo} seven`, labels: [] },
    { number: 8, title: `${repo} eight`, labels: [] },
  ] })),
  fetchDeferred: vi.fn(), fetchRepoLabels: vi.fn().mockResolvedValue({ labels: [] }),
  startAuthedInterval: vi.fn().mockReturnValue(() => {}),
  deferRow: vi.fn(), deferRowsBulk: vi.fn(), restoreRow: vi.fn(),
}));
afterEach(() => { vi.clearAllMocks(); vi.unstubAllGlobals(); });
const operations = ['defer', 'bulk-defer', 'restore', 'bulk-restore'] as const;
for (const kind of ['issue', 'pr'] as const) {
for (const op of operations) {
  for (const fails of [false, true]) {
    for (const returnToA of [false, true]) {
      for (const destinationNumber of [7, 8]) {
        it(`${kind} ${op}: ignores stale ${fails ? 'error' : 'success'} after ${returnToA ? 'A-B-A' : 'A-B'} switch, destination #${destinationNumber}`, async () => {
          vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
          const restoring = op.includes('restore');
          const host = document.createElement('div'); document.body.append(host);
          const root = createRoot(host);
          let settle!: (value: unknown) => void;
          let reject!: (error: Error) => void;
          const pending = new Promise((yes, no) => { settle = yes; reject = no; });
          const api = op.startsWith('bulk') ? deferRowsBulk : restoring ? restoreRow : deferRow;
          vi.mocked(api).mockReturnValue(pending as never);
          let visit = 0;
          vi.mocked(fetchDeferred).mockImplementation(async () => ({ deferred: restoring
            ? [{ kind, number: visit === 0 ? 7 : destinationNumber, deferred_at: 1 }] : [] }));
          const show = async (repo: string) => {
            await act(async () => root.render(<ReviewView repo={repo} onAskAgent={() => {}} onSelectPr={() => {}} />));
          };
          const click = async (id: string) => {
            const element = host.querySelector<HTMLInputElement>(`[data-testid="${id}"]`);
            expect(element, id).not.toBeNull();
            await act(async () => element!.click());
          };
          try {
            await show('repo-a');
            if (op.startsWith('bulk')) {
              await click(`${restoring ? 'restore' : 'defer'}-row-check-${kind}-7`);
              await click(restoring ? 'restore-bulk-restore' : 'defer-bulk-defer');
            } else await click(restoring ? `restore-deferred-${kind}-7` : `defer-${kind}-7`);
            visit++;
            await show('repo-b');
            if (returnToA) { visit++; await show('repo-a'); }
            // Destination selection belongs to this new repo visit.
            await click(`${restoring ? 'restore' : 'defer'}-row-check-${kind}-${destinationNumber}`);
            const before = host.textContent;
            await act(async () => {
              if (fails) reject(new Error('old repo mutation failed'));
              else settle({ deferred: restoring ? [] : [{ kind, number: 7, deferred_at: 1 }] });
            });
            expect(host.textContent).toBe(before);
            expect(host.querySelector<HTMLInputElement>(`[data-testid="${restoring ? 'restore' : 'defer'}-row-check-${kind}-${destinationNumber}"]`)?.checked).toBe(true);
          } finally { await act(async () => root.unmount()); host.remove(); }
        });
      }
    }
  }
  it(`${kind} ${op}: applies a successful settle without a repo switch`, async () => {
    vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
    const restoring = op.includes('restore');
    vi.mocked(fetchDeferred).mockResolvedValue({ deferred: restoring ? [{ kind, number: 7, deferred_at: 1 }] : [] });
    const api = op.startsWith('bulk') ? deferRowsBulk : restoring ? restoreRow : deferRow;
    let complete!: (value: unknown) => void;
    vi.mocked(api).mockReturnValue(new Promise(resolve => { complete = resolve; }) as never);
    const host = document.createElement('div'); document.body.append(host); const root = createRoot(host);
    const click = async (id: string) => { await act(async () => host.querySelector<HTMLInputElement>(`[data-testid="${id}"]`)!.click()); };
    try {
      await act(async () => root.render(<ReviewView repo="repo-a" onAskAgent={() => {}} onSelectPr={() => {}} />));
      if (op.startsWith('bulk')) {
        await click(`${restoring ? 'restore' : 'defer'}-row-check-${kind}-7`);
        await click(restoring ? 'restore-bulk-restore' : 'defer-bulk-defer');
      } else await click(restoring ? `restore-deferred-${kind}-7` : `defer-${kind}-7`);
      // An ordinary same-repo list refresh must not invalidate the mutation.
      const calls = vi.mocked(startAuthedInterval).mock.calls;
      const poll = calls[calls.length - 1][0];
      await act(async () => { poll(); });
      await act(async () => complete({ deferred: restoring ? [] : [{ kind, number: 7, deferred_at: 1 }] }));
      expect(host.querySelector(`[data-testid="review-deferred-${kind}-7"]`) !== null).toBe(!restoring);
      expect(host.textContent).toContain(restoring ? 'Restored' : 'Deferred');
    } finally { await act(async () => root.unmount()); host.remove(); }
  });
}
}
