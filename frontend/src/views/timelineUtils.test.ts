import { describe, expect, it } from 'vitest';
import { toTimelineItems } from './timelineUtils';
import type { AgentRun } from '../apiClient';

function turn(over: Partial<AgentRun> = {}): AgentRun {
  return {
    id: 't1',
    script: 'muse',
    model: 'qwen3:8b',
    provider: 'ollama',
    thread: 'th1',
    started_ts: 300,
    duration_s: 12,
    ok: true,
    tools: [],
    tokens: 1500,
    preview: 'did the thing',
    has_prompt: true,
    cost_usd: null,
    metered: false,
    ...over,
  };
}

describe('timeline merge (issue #812)', () => {
  it('interleaves runs, turns, pulls, and merges newest first', () => {
    const items = toTimelineItems(
      [{ id: 'r1', title: 'briefing', status: 'done', done: true, at: 100 }],
      [turn()],
      [{ kind: 'webhook', event: 'webhook pull ok', at: 200, ok: true }],
      [{ kind: 'pr-action', event: 'merge: PR #5 merged', ts: 400 }],
    );
    expect(items.map((i) => i.feed)).toEqual(['merge', 'turn', 'pull', 'run']);
    expect(items[0].summary).toContain('PR #5');
    expect(items[1].summary).toContain('Muse');
    expect(items[3].summary).toContain('Briefing');
  });

  it('returns no rows for empty feeds', () => {
    expect(toTimelineItems([], [], [], [])).toEqual([]);
  });

  it('sorts ageless rows last with an unknown-time label', () => {
    const items = toTimelineItems(
      [
        { id: 'ageless', title: 'briefing', status: 'done', done: true },
        { id: 'timed', title: 'briefing', status: 'done', done: true, at: 100 },
      ],
      [],
      [],
      [],
    );
    expect(items.map((i) => i.key)).toEqual(['run-timed', 'run-ageless']);
    expect(items[1].at).toContain('unknown time');
  });

  it('marks a nonzero-exit run as a failure', () => {
    const items = toTimelineItems(
      [{ id: 'r1', title: 'briefing', status: 'done', done: true, exit_code: 1, at: 100 }],
      [],
      [],
      [],
    );
    expect(items).toHaveLength(1);
    expect(items[0].tone).toBe('bad');
    expect(items[0].detail).toContain('exit: 1');
  });

  it('summarizes a turn like the Past runs line', () => {
    const items = toTimelineItems([], [turn()], [], []);
    expect(items).toHaveLength(1);
    expect(items[0].summary).toBe('Muse · qwen3:8b · ok · 12s · 1500 tok · free');
    expect(items[0].tone).toBe('ok');
    expect(items[0].at).not.toContain('unknown time');
  });

  it('marks a failed turn and quotes metered cost', () => {
    const items = toTimelineItems(
      [],
      [turn({ id: 't2', ok: false, metered: true, cost_usd: 0.01234, duration_s: null })],
      [],
      [],
    );
    expect(items[0].tone).toBe('bad');
    expect(items[0].summary).toContain('FAILED');
    expect(items[0].summary).toContain('$0.0123');
    expect(items[0].summary).not.toContain('12s');
  });

  it('keeps unrated metered turns an honest bare tag', () => {
    const items = toTimelineItems([], [turn({ metered: true, cost_usd: null })], [], []);
    expect(items[0].summary).toContain('metered');
    expect(items[0].summary).not.toContain('$');
  });

  it('words pulls like the matching History row', () => {
    const items = toTimelineItems(
      [],
      [],
      [{ kind: 'webhook', event: 'webhook pull failed: non-fast-forward update rejected', at: 200, ok: false }],
      [],
    );
    expect(items[0].summary).toContain('remote has new commits');
    expect(items[0].tone).toBe('bad');
  });
});
