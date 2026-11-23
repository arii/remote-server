// @vitest-environment happy-dom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '../apiClient';
import LedgerTriageView from './LedgerTriageView';

vi.mock('../apiClient', async (original) => ({
  ...await original<typeof import('../apiClient')>(),
  fetchLedgerIntake: vi.fn(),
  approveLedgerIntake: vi.fn(),
  mergeLedgerIntake: vi.fn(),
}));

let root: Root;
let host: HTMLDivElement;

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  host = document.createElement('div');
  document.body.append(host);
  root = createRoot(host);
  vi.mocked(api.fetchLedgerIntake).mockResolvedValue({
    rows: [{
      kind: 'ledger-intake',
      canonical_intake_id: 'intake-1',
      status: 'NEEDS_INFO',
      title: 'Add retry policy',
      missing_fields: ['scope'],
    }],
  });
  vi.mocked(api.approveLedgerIntake).mockResolvedValue({
    ok: true,
    row: { kind: 'ledger-intake', canonical_intake_id: 'intake-1', status: 'READY' },
  });
  vi.mocked(api.mergeLedgerIntake).mockResolvedValue({
    ok: true,
    row: { kind: 'ledger-intake', intake_id: 'intake-new', status: 'DUPLICATE' },
  });
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.resetAllMocks();
  vi.unstubAllGlobals();
});

describe('LedgerTriageView', () => {
  it('shows only flagged fields and clears a card after approval', async () => {
    await act(async () => root.render(<LedgerTriageView />));
    expect(host.textContent).toContain('Add retry policy');
    expect(host.querySelectorAll('input')).toHaveLength(1);
    expect(host.textContent).not.toContain('Done-when');

    await act(async () => {
      const input = host.querySelector('input')!;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, 'backend');
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await act(async () => {
      host.querySelector<HTMLButtonElement>('.btn-primary')!.click();
    });
    expect(api.approveLedgerIntake).toHaveBeenCalledWith('intake-1', { scope: 'backend' });
    expect(host.textContent).not.toContain('Add retry policy');
  });

  it('shows the matched row and merges it without creating a READY card', async () => {
    vi.mocked(api.fetchLedgerIntake).mockResolvedValueOnce({
      rows: [{
        kind: 'ledger-intake',
        intake_id: 'intake-new',
        canonical_intake_id: 'intake-new',
        status: 'SUSPECTED-DUPLICATE',
        title: 'Retry policy',
        duplicate_candidate: {
          intake_id: 'intake-old',
          canonical_intake_id: 'intake-old',
          title: 'Retry policy',
          status: 'READY',
          ts: 1_700_000_000,
        },
      }],
    });
    await act(async () => root.render(<LedgerTriageView />));
    expect(host.textContent).toContain('Possible match');
    expect(host.textContent).toContain('READY');
    await act(async () => {
      host.querySelectorAll<HTMLButtonElement>('.btn-primary')[0].click();
    });
    expect(api.mergeLedgerIntake).toHaveBeenCalledWith('intake-new', 'intake-old', '');
    expect(host.textContent).not.toContain('Retry policy');
  });
});
