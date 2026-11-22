import { describe, expect, it } from 'vitest';
import type { AgentRun } from '../apiClient';
import type { JobRecord, LedgerEntry } from '../apiTypes';
import { inRepoScope } from './historyUtils';
import { toTimelineItems } from './timelineUtils';

const job = (id: string, repo?: string): JobRecord =>
  ({ id, title: `job-${id}`, kind: 'run-td', repo, done: true, exit_code: 0, at: 1_700_000_000 }) as JobRecord;
const turn = (id: string, repo: string | null): AgentRun =>
  ({
    id, script: 'chat', model: 'm', provider: null, thread: 't', repo, started_ts: 1_700_000_100,
    duration_s: 1, ok: true, tools: [], tokens: 1, preview: 'hello', prompt_first_line: '',
    has_prompt: false, cost_usd: null, metered: false,
  }) as AgentRun;
const merge = (repo: string): LedgerEntry =>
  ({ kind: 'pr-action', repo, event: `merged ${repo}`, at: 1_700_000_200 }) as LedgerEntry;

describe('repo filter applies to every list (#1491 finding 5)', () => {
  it('keeps matching and repo-less rows only', () => {
    expect(inRepoScope('feels', 'feels')).toBe(true);
    expect(inRepoScope(undefined, 'feels')).toBe(true);
    expect(inRepoScope(null, 'feels')).toBe(true);
    expect(inRepoScope('other', 'feels')).toBe(false);
  });
  it('Activity drops other repos across runs, turns, and merges', () => {
    const items = toTimelineItems(
      [job('a', 'feels'), job('b', 'other'), job('g')],
      [turn('t1', 'feels'), turn('t2', 'other')],
      [],
      [merge('feels'), merge('other')],
      { repo: 'feels' },
    );
    expect(items.map((i) => i.key).sort()).toEqual(['merge-0', 'run-a', 'run-g', 'turn-t1']);
  });
  it('no repo option keeps everything', () => {
    expect(toTimelineItems([job('a', 'x'), job('b', 'y')], [], [], [])).toHaveLength(2);
  });
});

describe('search filters Activity too (#1491 finding 22)', () => {
  it('matches summary or detail, ignoring a leading #', () => {
    const jobs = [job('alpha'), job('beta')];
    expect(toTimelineItems(jobs, [], [], [], { query: 'alpha' }).map((i) => i.key)).toEqual(['run-alpha']);
    expect(toTimelineItems(jobs, [], [], [], { query: '#beta' }).map((i) => i.key)).toEqual(['run-beta']);
    expect(toTimelineItems(jobs, [], [], [], { query: 'zzz' })).toEqual([]);
  });
});
