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
  localStorage.clear();
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
  it('shows the title, short problem, originating issue link, and status on the same card', async () => {
    const sourceUrl = 'https://github.com/arii/remote-server/issues/1574';
    vi.mocked(api.fetchLedgerIntake).mockResolvedValueOnce({
      rows: [{
        kind: 'ledger-intake',
        intake_id: 'intake-mobile',
        status: 'NEW',
        title: 'Fix unreadable triage cards',
        problem: 'The issue link is missing on phones.',
        source_url: sourceUrl,
      }],
    });

    await act(async () => root.render(<LedgerTriageView />));
    const card = host.querySelector('.ledger-triage-card')!;
    expect(card.querySelector('h3')?.textContent).toBe('Fix unreadable triage cards');
    expect(card.textContent).toContain('The issue link is missing on phones.');
    expect(card.querySelector('a')?.getAttribute('href')).toBe(sourceUrl);
    expect(card.querySelector('.ledger-card-head')?.textContent).toContain('NEW');
    expect(card.querySelector('details')).toBeNull();
  });

  it('folds a long problem in the incoming row and retains the originating PR link', async () => {
    const problem = 'A long problem with an unbroken value: ' + 'x'.repeat(300);
    const sourceUrl = 'https://github.com/arii/remote-server/pull/1574';
    vi.mocked(api.fetchLedgerIntake).mockResolvedValueOnce({
      rows: [{ ...duplicateRows.rows[0], problem, source_url: sourceUrl }],
    });

    await act(async () => root.render(<LedgerTriageView />));
    const card = host.querySelector('.ledger-triage-card')!;
    const disclosure = card.querySelector('details')!;
    const preview = disclosure.querySelector('summary')!.textContent!;
    expect(disclosure.hasAttribute('open')).toBe(false);
    expect(preview).toMatch(/…$/);
    expect(problem.startsWith(preview.slice(0, -1))).toBe(true);
    expect(disclosure.querySelector('p')?.textContent).toBe(problem);
    expect(card.querySelector('a')?.getAttribute('href')).toBe(sourceUrl);
  });

  it('does not link a non-web source URL', async () => {
    vi.mocked(api.fetchLedgerIntake).mockResolvedValueOnce({
      rows: [{ ...duplicateRows.rows[0], source_url: 'javascript:void(0)' }],
    });

    await act(async () => root.render(<LedgerTriageView />));
    expect(host.querySelector('.ledger-triage-card a')).toBeNull();
  });

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

  it('switches to the inline confirm when the server flags a typed field', async () => {
    vi.mocked(api.approveLedgerIntake)
      .mockRejectedValueOnce(
        new Error('confirmation required: high blast radius (production_mutation)'),
      )
      .mockResolvedValueOnce({
        ok: true,
        row: { kind: 'ledger-intake', canonical_intake_id: 'intake-1', status: 'READY' },
      });
    await act(async () => root.render(<LedgerTriageView />));
    await act(async () => {
      const input = host.querySelector('input')!;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, 'migrate the prod DB');
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await act(async () => {
      host.querySelector<HTMLButtonElement>('.btn-primary')!.click();
    });
    expect(api.approveLedgerIntake).toHaveBeenLastCalledWith('intake-1', { scope: 'migrate the prod DB' }, false);
    expect(host.textContent).toContain('production mutation');

    await act(async () => {
      host.querySelector<HTMLButtonElement>('[data-testid^="approve-high-risk"]')!.click();
    });
    await act(async () => {
      host.querySelector<HTMLButtonElement>('button[aria-label^="Confirm"]')!.click();
    });
    expect(api.approveLedgerIntake).toHaveBeenLastCalledWith('intake-1', { scope: 'migrate the prod DB' }, true);
  });
});

describe('LedgerTriageView review markers', () => {
  const datedRows = {
    rows: [
      { kind: 'ledger-intake', intake_id: 'old', status: 'NEW', title: 'Older row', ts: 1_700_000_000 },
      { kind: 'ledger-intake', intake_id: 'new', status: 'NEW', title: 'Newer row', ts: 1_700_000_500 },
      { kind: 'ledger-intake', intake_id: 'mid', status: 'NEW', title: 'Middle row', at: '2023-11-14T22:20:00Z' },
    ],
  };

  function titles(): string[] {
    return Array.from(host.querySelectorAll('.ledger-triage-card h3')).map((h) => h.textContent || '');
  }

  function count(): string {
    return host.querySelector('[data-testid="ledger-unreviewed-count"]')!.textContent || '';
  }

  it('lists rows newest-first and counts every row as unreviewed', async () => {
    vi.mocked(api.fetchLedgerIntake).mockResolvedValueOnce(datedRows);
    await act(async () => root.render(<LedgerTriageView />));
    expect(titles()).toEqual(['Newer row', 'Middle row', 'Older row']);
    expect(count()).toBe('3 unreviewed');
    expect(host.querySelectorAll('[data-testid^="unreviewed-"]')).toHaveLength(3);
  });

  it('clears the marker when a card is opened and keeps it cleared on reload', async () => {
    vi.mocked(api.fetchLedgerIntake).mockResolvedValue(datedRows);
    await act(async () => root.render(<LedgerTriageView />));
    await act(async () => host.querySelector<HTMLElement>('.ledger-triage-card')!.click());
    expect(tap('unreviewed-new')).toBeNull();
    expect(count()).toBe('2 unreviewed');

    await act(async () => root.unmount());
    root = createRoot(host);
    await act(async () => root.render(<LedgerTriageView />));
    expect(tap('unreviewed-new')).toBeNull();
    expect(count()).toBe('2 unreviewed');
  });

  it('clears the marker when a card is opened from the keyboard', async () => {
    vi.mocked(api.fetchLedgerIntake).mockResolvedValueOnce(datedRows);
    await act(async () => root.render(<LedgerTriageView />));
    await act(async () => {
      host.querySelector('.ledger-triage-card')!.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }),
      );
    });
    expect(tap('unreviewed-new')).toBeNull();
    expect(count()).toBe('2 unreviewed');
  });

  it('counts a dismissed row as reviewed', async () => {
    await act(async () => root.render(<LedgerTriageView />));
    expect(count()).toBe('1 unreviewed');
    await act(async () => {
      const input = host.querySelector('input')!;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, 'backend');
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await act(async () => host.querySelector<HTMLButtonElement>('.btn-primary')!.click());
    expect(count()).toBe('0 unreviewed');
    expect(JSON.parse(localStorage.getItem('ledgerTriage.reviewed') || '[]')).toEqual(['intake-1']);
  });
});
