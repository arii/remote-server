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
});
