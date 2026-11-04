import { describe, expect, it } from 'vitest';
import type { DispatchItem } from '../apiTypes';
import {
  groupQueueByRepo,
  nextDispatchable,
  queueListActive,
  queueRowActions,
  queueRowMeta,
  splitBlockReasons,
} from './queueUtils';

function mkItem(over: Partial<DispatchItem> = {}): DispatchItem {
  return { repo: 'remote-server', issue: 1, state: 'queued', detail: '', ...over };
}

describe('groupQueueByRepo (issue #853)', () => {
  it('groups by repo, sorted, queued first then by issue', () => {
    const groups = groupQueueByRepo([
      mkItem({ repo: 'tech-dancer', issue: 4850, state: 'queued' }),
      mkItem({ repo: 'remote-server', issue: 2, state: 'done' }),
      mkItem({ repo: 'tech-dancer', issue: 4848, state: 'running' }),
      mkItem({ repo: 'remote-server', issue: 1, state: 'queued' }),
    ]);
    expect(groups.map((g) => g.repo)).toEqual(['remote-server', 'tech-dancer']);
    expect(groups[0].items.map((i) => i.issue)).toEqual([1, 2]);
    expect(groups[1].items.map((i) => i.issue)).toEqual([4850, 4848]);
  });

  it('returns no groups for an empty queue', () => {
    expect(groupQueueByRepo([])).toEqual([]);
  });
});

describe('queueListActive (issue #853)', () => {
  it('is active while any item is queued or running', () => {
    expect(queueListActive([mkItem({ state: 'queued' })])).toBe(true);
    expect(queueListActive([mkItem({ state: 'running' })])).toBe(true);
    expect(queueListActive([mkItem({ state: 'done' })])).toBe(false);
    expect(queueListActive([mkItem({ state: 'failed' })])).toBe(false);
    expect(queueListActive([])).toBe(false);
  });
});

describe('nextDispatchable (issue #853)', () => {
  it('picks the first queued row, null when nothing is queued', () => {
    expect(
      nextDispatchable({ repo: 'r', items: [mkItem({ state: 'running' }), mkItem({ issue: 7 })] }),
    ).toMatchObject({ issue: 7 });
    expect(nextDispatchable({ repo: 'r', items: [mkItem({ state: 'done' })] })).toBeNull();
  });
});

describe('queueRowMeta (issue #853)', () => {
  it('keeps live rows terse and surfaces terminal detail', () => {
    expect(queueRowMeta(mkItem({ state: 'queued' }))).toBe('queued');
    expect(queueRowMeta(mkItem({ state: 'running' }))).toBe('running');
    expect(queueRowMeta(mkItem({ state: 'failed', detail: 'exit 3' }))).toBe('failed · exit 3');
    expect(queueRowMeta(mkItem({ state: 'done', detail: '' }))).toBe('done');
  });
});

describe('queueRowActions (issue #853)', () => {
  it('dispatches queued rows, waits on running, retries terminal', () => {
    expect(queueRowActions('queued')).toEqual({ dispatch: true, cancel: true });
    expect(queueRowActions('running')).toEqual({ dispatch: false, cancel: false });
    expect(queueRowActions('failed')).toEqual({ dispatch: true, cancel: true });
    expect(queueRowActions('done')).toEqual({ dispatch: true, cancel: true });
  });
});

describe('splitBlockReasons (issue #853)', () => {
  it('splits the joined 409 detail back into lines', () => {
    expect(splitBlockReasons('a; b ;c')).toEqual(['a', 'b', 'c']);
    expect(splitBlockReasons('')).toEqual([]);
  });
});
