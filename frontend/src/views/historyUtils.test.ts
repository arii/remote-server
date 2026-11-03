import { describe, expect, it } from 'vitest';
import { toHistoryRows } from './historyUtils';

const TURN_DETAIL = [
  'asked: list files',
  'ran: muse turn (fresh session)',
  'verdict: exit 0 in 1.2s',
  'said: yo',
].join('\n');

describe('agent-turn decisions (issue #414)', () => {
  it('expands the History entry to the recorded decision lines', () => {
    const rows = toHistoryRows([], [
      {
        kind: 'agent-turn',
        session_id: 's-dec',
        prompt: 'list files',
        exit: 0,
        detail: TURN_DETAIL,
        ts: 1700000000,
      },
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0].detail).toContain('asked: list files');
    expect(rows[0].detail).toContain('verdict: exit 0');
    expect(rows[0].members[0].detail).toContain('said: yo');
  });

  it('summarizes a failed turn with the recorded reason', () => {
    const rows = toHistoryRows([], [
      {
        kind: 'agent-turn',
        session_id: 's-fail',
        exit: 3,
        error: 'muse turn failed (exit 3): oops',
        detail: 'asked: hi\nverdict: exit 3 in 1.0s\nwhy: oops',
        ts: 1700000000,
      },
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0].summary).toContain('oops');
    expect(rows[0].tone).toBe('bad');
  });
});
