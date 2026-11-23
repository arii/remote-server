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
  distinctLedgerIntake: vi.fn(),
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

const duplicateRows = {
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
};

function tap(testid: string): HTMLButtonElement {
  return host.querySelector<HTMLButtonElement>(`[data-testid="${testid}"]`)!;
}

function confirmButton(label: string): HTMLButtonElement {
  return host.querySelector<HTMLButtonElement>(`button[aria-label^="Confirm ${label}"]`)!;
}

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
    expect(api.approveLedgerIntake).toHaveBeenCalledWith('intake-1', { scope: 'backend' }, false);
    expect(host.textContent).not.toContain('Add retry policy');
  });

  it('shows the matched row and merges only after the confirm tap', async () => {
    vi.mocked(api.fetchLedgerIntake).mockResolvedValueOnce(duplicateRows);
    await act(async () => root.render(<LedgerTriageView />));
    expect(host.textContent).toContain('Possible match');
    expect(host.textContent).toContain('READY');
    await act(async () => tap('merge-intake-new').click());
    expect(api.mergeLedgerIntake).not.toHaveBeenCalled();
    expect(host.textContent).toContain('Merge duplicate Retry policy?');
    await act(async () => confirmButton('Merge duplicate').click());
    expect(api.mergeLedgerIntake).toHaveBeenCalledWith('intake-new', 'intake-old', '');
    expect(host.textContent).not.toContain('Possible match');
  });

  it('marks distinct only after the confirm tap', async () => {
    vi.mocked(api.fetchLedgerIntake).mockResolvedValueOnce(duplicateRows);
    vi.mocked(api.distinctLedgerIntake).mockResolvedValue({
      ok: true,
      row: { kind: 'ledger-intake', intake_id: 'intake-new', status: 'NEW' },
    });
    await act(async () => root.render(<LedgerTriageView />));
    await act(async () => tap('distinct-intake-new').click());
    expect(api.distinctLedgerIntake).not.toHaveBeenCalled();
    await act(async () => confirmButton('Not a duplicate').click());
    expect(api.distinctLedgerIntake).toHaveBeenCalledWith('intake-new', 'intake-old');
    expect(api.mergeLedgerIntake).not.toHaveBeenCalled();
  });

  it('hides Approve on a quarantined DUPLICATE row', async () => {
    vi.mocked(api.fetchLedgerIntake).mockResolvedValueOnce({
      rows: [{ ...duplicateRows.rows[0], status: 'DUPLICATE' }],
    });
    await act(async () => root.render(<LedgerTriageView />));
    expect(host.textContent).not.toContain('Approve and mark READY');
    expect(host.textContent).toContain('before approving');
    expect(tap('merge-intake-new')).toBeTruthy();
  });

  it('names the risk categories and sends the confirm only after the inline confirm', async () => {
    vi.mocked(api.fetchLedgerIntake).mockResolvedValueOnce({
      rows: [{
        kind: 'ledger-intake',
        canonical_intake_id: 'intake-risk',
        status: 'NEEDS_APPROVAL',
        title: 'Deploy the backend',
        risk_categories: ['production_mutation'],
      }],
    });
    await act(async () => root.render(<LedgerTriageView />));
    expect(host.textContent).toContain('NEEDS_APPROVAL');
    expect(host.textContent).toContain('production mutation');

    await act(async () => {
      host.querySelector<HTMLButtonElement>('[data-testid^="approve-high-risk"]')!.click();
    });
    expect(api.approveLedgerIntake).not.toHaveBeenCalled();
    expect(host.querySelector('[data-testid="tap-confirm"]')!.textContent).toContain('production mutation');
    await act(async () => {
      host.querySelector<HTMLButtonElement>('button[aria-label^="Confirm"]')!.click();
    });
    expect(api.approveLedgerIntake).toHaveBeenCalledWith('intake-risk', {}, true);
  });
});
