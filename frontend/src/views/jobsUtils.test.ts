import { describe, expect, it } from 'vitest';
import {
  completionOutcome,
  conflictBlockReason,
  fixCiBlockReason,
  mergeNoteFor,
  prFieldSeed,
  repairFailureMessage,
  tdBlockReason,
} from './jobsUtils';

describe('td block reasons', () => {
  it('fails open when PR state is unknown', () => {
    expect(fixCiBlockReason(null)).toBeNull();
    expect(conflictBlockReason(null)).toBeNull();
    expect(tdBlockReason('fix-ci', null)).toBeNull();
    expect(tdBlockReason('resolve-conflicts', null)).toBeNull();
  });

  it('gates fix-ci on failing CI', () => {
    expect(fixCiBlockReason({ n: 1, ci: 'failing', conflicted: false })).toBeNull();
    expect(fixCiBlockReason({ n: 1, ci: 'passing', conflicted: false })).toBe(
      'CI is passing — nothing to fix.',
    );
  });

  it('gates resolve-conflicts on conflicted state', () => {
    expect(conflictBlockReason({ n: 1, ci: 'failing', conflicted: true })).toBeNull();
    expect(conflictBlockReason({ n: 1, ci: 'failing', conflicted: false })).toBe(
      'No conflicts on this PR.',
    );
  });

  it('leaves read-only commands ungated', () => {
    const pr = { n: 1, ci: 'passing', conflicted: false };
    expect(tdBlockReason('read-reviews', pr)).toBeNull();
    expect(tdBlockReason('list-sessions', pr)).toBeNull();
  });
});

describe('mergeNoteFor', () => {
  it('returns null for other actions or unknown readiness', () => {
    expect(mergeNoteFor('recreate', 3)).toBeNull();
    expect(mergeNoteFor('merge-approved', null)).toBeNull();
  });

  it('reports the ready count', () => {
    expect(mergeNoteFor('merge-approved', 0)).toBe('No approved, green PRs right now.');
    expect(mergeNoteFor('merge-approved', 2)).toBe('2 ready to merge.');
  });
});

describe('repairFailureMessage', () => {
  it('returns null unless the run failed', () => {
    expect(repairFailureMessage('done', 7, 0)).toBeNull();
    expect(repairFailureMessage('running', 7, null)).toBeNull();
  });

  it('names the issue and exit code', () => {
    expect(repairFailureMessage('failed', 7, 2)).toBe('Repair run for #7 failed (exit 2).');
    expect(repairFailureMessage('failed', null, null)).toBe('Repair run for #? failed.');
  });
});

describe('completionOutcome', () => {
  it('reports failures with the result line', () => {
    expect(
      completionOutcome({
        runId: 'abc123',
        action: 'merge-approved',
        output: '$ td merge\nmerged 2 PRs',
        exitCode: 1,
        label: 'Merge approved PR',
        repo: 'r',
      }),
    ).toEqual({ notice: 'Run abc123 failed (exit 1): merged 2 PRs', planCard: null });
  });

  it('ends the failure notice with a stop when nothing surfaced', () => {
    expect(
      completionOutcome({
        runId: 'abc123',
        action: 'merge-approved',
        output: '',
        exitCode: 1,
        label: 'Merge approved PR',
        repo: 'r',
      }),
    ).toEqual({ notice: 'Run abc123 failed (exit 1).', planCard: null });
  });

  it('reports success with the result line', () => {
    expect(
      completionOutcome({
        runId: 'abc123',
        action: 'merge-approved',
        output: 'merged 2 PRs',
        exitCode: 0,
        label: 'Merge approved PR',
        repo: 'r',
      }),
    ).toEqual({ notice: 'Finished Merge approved PR: merged 2 PRs', planCard: null });
  });

  it('falls back to a bare label and Run name', () => {
    expect(
      completionOutcome({
        runId: 'abc123',
        action: 'merge-approved',
        output: '',
        exitCode: null,
        label: '',
        repo: 'r',
      }),
    ).toEqual({ notice: 'Finished Run.', planCard: null });
  });

  it('builds the plan card only for create-plan result lines', () => {
    const plan = completionOutcome({
      runId: 'abc123',
      action: 'create-plan',
      output: 'wrote .agents/plans/2026-11-03.md',
      exitCode: 0,
      label: 'Write briefing plan',
      repo: 'r',
    });
    expect(plan.planCard).toEqual({ repo: 'r', name: '2026-11-03.md' });
    const other = completionOutcome({
      runId: 'abc123',
      action: 'merge-approved',
      output: 'wrote .agents/plans/2026-11-03.md',
      exitCode: 0,
      label: 'Merge approved PR',
      repo: 'r',
    });
    expect(other.planCard).toBeNull();
  });
});

describe('pr field seed', () => {
  it('seeds the field from the selected row, empty with no selection', () => {
    expect(prFieldSeed(7)).toBe('7');
    expect(prFieldSeed(null)).toBe('');
  });
});
