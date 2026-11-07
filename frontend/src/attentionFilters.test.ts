import { describe, expect, it } from 'vitest';
import {
  dedupAttentionLines,
  deriveCardLabels,
  failureNameSlot,
  isActionableAttention,
  isActionableLine,
  isNonRequiredWorkflowFailure,
} from './attentionFilters';
import type { AttentionItem } from './attentionTypes';

const item = (kind: string, title: string): AttentionItem => ({
  id: `${kind}-${title}`,
  kind,
  title,
  detail: '',
  repo: 'remote-server',
  severity: 'warn',
  action: { label: 'x', method: 'GET', href: 'api/x', destructive: false, intent: 'x' },
  created_at: 1,
});

describe('non-required workflow filter (issue #1109.1)', () => {
  it('matches the console-e2e workflow by name', () => {
    expect(isNonRequiredWorkflowFailure('Failing workflow: console-e2e')).toBe(true);
    expect(isNonRequiredWorkflowFailure('Failing workflow: console E2E smoke')).toBe(true);
    expect(isNonRequiredWorkflowFailure('Failing workflow: CI')).toBe(false);
    expect(isNonRequiredWorkflowFailure('- failing: console-e2e on main (push)')).toBe(true);
  });
  it('drops only failing-run items for that workflow', () => {
    expect(isActionableAttention(item('failing-run', 'Failing workflow: console-e2e'))).toBe(false);
    expect(isActionableAttention(item('failing-run', 'Failing workflow: CI'))).toBe(true);
    expect(isActionableAttention(item('failed-job', 'Failed job: console-e2e build'))).toBe(true);
    expect(isActionableAttention(item('pending-approval', 'Approve: deploy'))).toBe(true);
  });
  it('applies the same rule to briefing strings', () => {
    expect(isActionableLine('- failing: console-e2e on main (push) — https://x/y')).toBe(false);
    expect(isActionableLine('Failing run: console-e2e on main (push) — likely cause: z')).toBe(false);
    expect(isActionableLine('- failing: CI on main (push) — failed: t')).toBe(true);
  });
  it('matches the run-name slot, never branch or cause text', () => {
    expect(failureNameSlot('- failing: CI on console-e2e-flake-fix (push)')).toBe('CI');
    expect(
      failureNameSlot('Failing run: CI on main (push) — likely cause: console-e2e (…)'),
    ).toBe('CI');
    expect(failureNameSlot('unprefixed line stays whole')).toBe('unprefixed line stays whole');
    // A required failure stays visible even when the branch or the
    // cause mentions console-e2e; only the name filters.
    expect(isActionableLine('- failing: CI on console-e2e-flake-fix (push)')).toBe(true);
    expect(
      isActionableLine('Failing run: CI on main (push) — likely cause: console-e2e (…)'),
    ).toBe(true);
    expect(isActionableLine('- failing: console E2E smoke on main (push)')).toBe(false);
    expect(
      isActionableAttention(item('failing-run', 'Failing workflow: console E2E smoke')),
    ).toBe(false);
  });
});

describe('attention dedup (issue #1109.5)', () => {
  const runs = ['- failing: console-e2e on main (push) — failed: t — https://x/y'];
  it('drops run-level attention lines when the run section owns them', () => {
    expect(
      dedupAttentionLines(
        ['Failing run: console-e2e on main (push) — likely cause: t', 'PR #1 — CI failing'],
        runs,
      ),
    ).toEqual(['PR #1 — CI failing']);
  });
  it('keeps attention lines as fallback without run data', () => {
    const attention = ['Failing run: CI on main (push) — likely cause: t'];
    expect(dedupAttentionLines(attention, [])).toEqual(attention);
    expect(dedupAttentionLines(attention, ['Workflow run list unavailable: boom'])).toEqual(attention);
  });
});

describe('deriveCardLabels (issue #1109.8)', () => {
  it('derives the label from a bracket tag when the label probe fell back', () => {
    expect(deriveCardLabels('Attention', ['[CI] 2 failing: build, e2e'])).toBe('CI');
  });
  it('leaves a real label untouched', () => {
    expect(deriveCardLabels('PR #1 — CI failing', ['[CI] 2 failing'])).toBe('PR #1 — CI failing');
  });
  it('keeps the fallback when no line carries a tag', () => {
    expect(deriveCardLabels('Attention', ['plain line'])).toBe('Attention');
  });
});
