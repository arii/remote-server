import { describe, expect, it } from 'vitest';
import { historyRowMatches, kindBadge, sentenceCase, toHistoryRows } from './historyUtils';
import type { HistoryRow } from './historyUtils';

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

describe('agent-turn latency (issue #801)', () => {
  it('details the recorded duration when present', () => {
    const rows = toHistoryRows([], [
      {
        kind: 'agent-turn',
        session_id: 's-lat',
        provider: 'ollama',
        duration_s: 12.5,
        exit: 0,
        detail: TURN_DETAIL,
        ts: 1700000000,
      },
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0].detail).toContain('duration: 12.5s');
    expect(rows[0].tone).toBe('ok');
  });

  it('drops duration and cost when the turn records neither', () => {
    const rows = toHistoryRows([], [
      {
        kind: 'agent-turn',
        session_id: 's-bare',
        exit: 0,
        detail: TURN_DETAIL,
        ts: 1700000000,
      },
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0].detail).not.toContain('duration:');
    expect(rows[0].detail).not.toContain('cost:');
  });
});

describe('collapsed outcome clauses (issue #423)', () => {
  it('shows the skipped reason on a repeated webhook row', () => {
    const rows = toHistoryRows(
      [],
      [0, 1, 2, 3].map((i) => ({
        kind: 'webhook',
        repo: 'remote-server',
        ok: true,
        detail: 'skipped: tree not ff-able',
        ts: 1700000000 - i,
      })),
    );
    expect(rows).toHaveLength(1);
    expect(rows[0].summary).toContain('skipped: tree not ff-able');
    expect(rows[0].summary).toMatch(/×4/);
  });

  it('names provider and task on agent-turn rows', () => {
    const rows = toHistoryRows([], [
      {
        kind: 'agent-turn',
        session_id: 's-1',
        provider: 'ollama',
        prompt: 'list files',
        exit: 0,
        detail: TURN_DETAIL,
        ts: 1700000000,
      },
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0].summary).toContain('ollama');
    expect(rows[0].summary).toContain('list files');
  });

  it('names the td command on run rows', () => {
    const rows = toHistoryRows([], [
      {
        kind: 'run-td',
        command: 'td --json preflight',
        ts: 1700000000,
      },
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0].summary).toContain('td --json preflight');
  });

  it('leaves routine rows bare', () => {
    const rows = toHistoryRows([], [
      { kind: 'chat', event: 'chat started', prompt: 'hi', ts: 2 },
      { kind: 'chat', event: 'chat finished', prompt: 'hi', ts: 1 },
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0].summary).toMatch(/Chat session activity ×2/);
  });

  it('keeps backend error summaries verbatim without a clause', () => {
    const rows = toHistoryRows([], [
      {
        kind: 'agent-turn',
        session_id: 's-f',
        exit: 3,
        error: 'muse turn failed (exit 3): oops',
        detail: 'asked: hi\nverdict: exit 3 in 1.0s\nwhy: oops',
        ts: 1700000000,
      },
    ]);
    // #572 sentence-cases the first letter at display; the rest of
    // the error stays verbatim and no outcome clause is appended.
    expect(rows[0].summary).toBe('Muse turn failed (exit 3): oops');
  });

  it('splits rows whose outcomes differ', () => {
    const rows = toHistoryRows([], [
      { kind: 'webhook', detail: 'skipped: tree dirty', ts: 2 },
      { kind: 'webhook', detail: 'up to date', ts: 1 },
    ]);
    expect(rows).toHaveLength(2);
    expect(rows[0].summary).toContain('skipped: tree dirty');
    expect(rows[1].summary).toContain('up to date');
  });
});

describe('summary casing (issue #572)', () => {
  it('capitalizes the first letter and leaves the rest verbatim', () => {
    expect(sentenceCase('chat started')).toBe('Chat started');
    expect(sentenceCase('Chat session activity ×3')).toBe('Chat session activity ×3');
    expect(sentenceCase('webhook — skipped: tree not ff-able')).toBe(
      'Webhook — skipped: tree not ff-able',
    );
    expect(sentenceCase('')).toBe('');
  });

  it('renders every collapsed summary capitalized', () => {
    const rows = toHistoryRows([], [
      { kind: 'webhook', detail: 'up to date', ts: 3 },
      { kind: 'chat', event: 'chat started', prompt: 'hi', ts: 2 },
      { kind: 'chat', event: 'chat finished', prompt: 'hi', ts: 1 },
    ]);
    expect(rows).toHaveLength(2);
    for (const row of rows) {
      expect(row.summary.charAt(0)).toBe(row.summary.charAt(0).toUpperCase());
    }
    expect(rows[1].summary).toMatch(/Chat session activity ×2/);
  });

  it('keeps grouping and counts unchanged', () => {
    const rows = toHistoryRows(
      [],
      [0, 1, 2].map((i) => ({ kind: 'webhook', detail: 'up to date', ts: 3 - i })),
    );
    expect(rows).toHaveLength(1);
    expect(rows[0].summary).toBe('Webhook — up to date ×3');
  });
});

describe('history text search (issue #608)', () => {
  function mkRow(over: Partial<HistoryRow> = {}): HistoryRow {
    return {
      key: 'k',
      kind: 'webhook',
      summary: 'Webhook — skipped: tree not ff-able',
      at: 'then',
      repeat: 1,
      recovery: '',
      tone: 'ok',
      detail: 'event: webhook',
      members: [{ at: 'then', detail: 'session: abc123' }],
      ...over,
    };
  }

  it('matches summary substrings case-insensitively', () => {
    expect(historyRowMatches(mkRow(), 'ff-able')).toBe(true);
    expect(historyRowMatches(mkRow(), 'SKIPPED')).toBe(true);
    expect(historyRowMatches(mkRow(), 'zzz-absent')).toBe(false);
  });

  it('matches kind and member detail, not just the summary', () => {
    expect(historyRowMatches(mkRow(), 'webhook')).toBe(true);
    expect(historyRowMatches(mkRow(), 'abc123')).toBe(true);
    expect(
      historyRowMatches(
        mkRow({ members: [{ at: 'a', detail: 'first' }, { at: 'b', detail: 'second hit' }] }),
        'second hit',
      ),
    ).toBe(true);
    expect(historyRowMatches(mkRow(), 'chat')).toBe(false);
  });

  it('matches everything on empty query', () => {
    expect(historyRowMatches(mkRow(), '')).toBe(true);
    expect(historyRowMatches(mkRow(), '   ')).toBe(true);
  });
});

describe('cli-turn rows (issue #590)', () => {
  const cli = {
    kind: 'cli-turn',
    ts: 1700000000,
    script: 'ask',
    model: 'qwen',
    thread_id: 't1',
    duration_s: 12.5,
    usage: { input_tokens: 100, output_tokens: 50, total_tokens: 150 },
    cost_usd: 0.0012,
    metered: false,
    tools: ['shell'],
  };

  it('badges cli-turn rows as cli, everything else verbatim', () => {
    expect(kindBadge('cli-turn')).toBe('cli');
    expect(kindBadge('run')).toBe('run');
    expect(kindBadge('chat')).toBe('chat');
  });

  it('summarizes script, model, duration, tokens, and cost', () => {
    const rows = toHistoryRows([], [cli]);
    expect(rows).toHaveLength(1);
    expect(rows[0].summary).toContain('Ask');
    expect(rows[0].summary).toContain('qwen');
    expect(rows[0].summary).toContain('12.5s');
    expect(rows[0].summary).toContain('150 tok');
    expect(rows[0].kind).toBe('cli-turn');
  });

  it('details thread, tools, and usage without failing the row', () => {
    const rows = toHistoryRows([], [cli]);
    expect(rows[0].detail).toContain('t1');
    expect(rows[0].detail).toContain('shell');
    expect(rows[0].detail).toContain('150');
    expect(rows[0].tone).toBe('ok');
  });

  it('tolerates missing usage and cost', () => {
    const rows = toHistoryRows([], [{ kind: 'cli-turn', ts: 1700000000 }]);
    expect(rows).toHaveLength(1);
    expect(rows[0].tone).toBe('ok');
  });
});

describe('exit-aware failures (issue #473)', () => {
  const failedJob = (id: string, at: number) => ({
    id,
    kind: 'run-td',
    name: 'td preflight',
    done: true,
    exit_code: 1,
    at,
  });

  it('renders a nonzero-exit job as tone bad with recovery', () => {
    const rows = toHistoryRows([failedJob('a1', 3000)], []);
    expect(rows).toHaveLength(1);
    expect(rows[0].tone).toBe('bad');
    expect(rows[0].recovery).not.toBe('');
    expect(rows[0].detail).toContain('exit: 1');
  });

  it('collapses repeated equal job failures into one incident row', () => {
    const rows = toHistoryRows(
      [failedJob('a3', 3000), failedJob('a2', 2000), failedJob('a1', 1000)],
      [],
    );
    expect(rows).toHaveLength(1);
    expect(rows[0].tone).toBe('bad');
    expect(rows[0].summary).toMatch(/×3/);
    expect(rows[0].summary).toContain('same failure repeated');
    expect(rows[0].recovery).not.toBe('');
  });

  it('leaves exit-0 and pending jobs green', () => {
    const rows = toHistoryRows(
      [
        { id: 'ok', kind: 'run-td', name: 'td preflight', done: true, exit_code: 0, at: 3000 },
        { id: 'run', kind: 'run-td', name: 'td lint', done: false, exit_code: null, at: 2000 },
        { id: 'early', kind: 'run-td', name: 'td test', done: false, exit_code: 1, at: 1000 },
      ],
      [],
    );
    expect(rows).toHaveLength(3);
    for (const row of rows) {
      expect(row.tone).toBe('ok');
      expect(row.recovery).toBe('');
    }
  });

  it('keeps the status regex for jobs without exits', () => {
    const rows = toHistoryRows(
      [{ id: 's1', kind: 'job', name: 'nightly', status: 'failed', done: true, at: 1000 }],
      [],
    );
    expect(rows).toHaveLength(1);
    expect(rows[0].tone).toBe('bad');
  });

  it('renders a bare nonzero ledger exit as tone bad with recovery', () => {
    const rows = toHistoryRows([], [
      { kind: 'run', action: 'logs', exit: 1, ts: 3000 },
      { kind: 'run', action: 'logs', exit: 1, ts: 2000 },
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0].tone).toBe('bad');
    expect(rows[0].summary).toMatch(/×2/);
    expect(rows[0].summary).toContain('same failure repeated');
    expect(rows[0].recovery).not.toBe('');
  });
});
