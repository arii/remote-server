import { describe, expect, it } from 'vitest';
import { subscribeJobsPush } from './apiClient';

// Type-boundary regression (boomtick #1222 item 6): jobs SSE frames
// must narrow each element like parseFrameProgress does for
// done/output — rows without a string id are dropped, valid rows
// pass through untouched, and malformed frames never reach
// subscribeJobsPush's onJobs.

interface FakeSource {
  url: string;
  listeners: Record<string, Array<(e: { data: string }) => void>>;
  onmessage: ((e: { data: string }) => void) | null;
  onerror: (() => void) | null;
  closed: boolean;
  addEventListener(type: string, fn: (e: { data: string }) => void): void;
  close(): void;
  emit(type: string, data: string): void;
  emitMessage(data: string): void;
}

function makeFactory(seen: FakeSource[]) {
  return (url: string): FakeSource => {
    const src: FakeSource = {
      url,
      listeners: {},
      onmessage: null,
      onerror: null,
      closed: false,
      addEventListener(type, fn) {
        (src.listeners[type] ??= []).push(fn);
      },
      close() {
        src.closed = true;
      },
      emit(type, data) {
        for (const fn of src.listeners[type] ?? []) fn({ data });
      },
      emitMessage(data) {
        src.onmessage?.({ data });
      },
    };
    seen.push(src);
    return src;
  };
}

function subscribe(seen: FakeSource[], jobs: unknown[], live: boolean[]) {
  return subscribeJobsPush(
    {
      onJobs: (j) => jobs.push(j),
      onLive: (l) => live.push(l),
    },
    makeFactory(seen),
  );
}

// Verbatim pre-fix parseFrameJobs (HEAD frontend/src/apiClient.ts):
// the bare `as JobRecord[]` cast let every row through, whatever
// its shape. Kept as the red oracle — the fixed filter must do
// strictly better on the same frame.
function preFixParseFrameJobs(data: string): unknown[] | null {
  try {
    const body = JSON.parse(data) as { jobs?: unknown };
    return Array.isArray(body.jobs) ? (body.jobs as unknown[]) : null;
  } catch {
    return null;
  }
}

const MIXED_FRAME = JSON.stringify({
  jobs: [
    { id: 'a' },
    { id: 'b', title: 'Bedtime', status: 'running', repo: 'r' },
    42,
    null,
    { noid: 1 },
    { id: 7 },
    { id: null },
    'x',
    ['y'],
  ],
});

describe('jobs frame boundary (boomtick #1222 item 6)', () => {
  it('keeps valid rows and drops rows without a string id', () => {
    const seen: FakeSource[] = [];
    const jobs: unknown[] = [];
    const live: boolean[] = [];
    subscribe(seen, jobs, live);
    seen[0].emit('jobs', MIXED_FRAME);
    expect(jobs).toEqual([
      [{ id: 'a' }, { id: 'b', title: 'Bedtime', status: 'running', repo: 'r' }],
    ]);
    expect(live).toEqual([true]);
  });

  it('delivers an empty list rather than swallowing when every row is malformed', () => {
    const seen: FakeSource[] = [];
    const jobs: unknown[] = [];
    const live: boolean[] = [];
    subscribe(seen, jobs, live);
    seen[0].emit('jobs', JSON.stringify({ jobs: [42, null, { noid: 1 }] }));
    expect(jobs).toEqual([[]]);
    expect(live).toEqual([true]);
  });

  it('ignores non-list and non-JSON frames without touching handlers', () => {
    const seen: FakeSource[] = [];
    const jobs: unknown[] = [];
    const live: boolean[] = [];
    subscribe(seen, jobs, live);
    seen[0].emit('jobs', JSON.stringify({ jobs: { id: 'a' } }));
    seen[0].emit('jobs', JSON.stringify({}));
    seen[0].emit('jobs', 'not json');
    expect(jobs).toEqual([]);
    expect(live).toEqual([]);
  });

  it('filters unnamed (onmessage) frames the same way as named jobs frames', () => {
    const seen: FakeSource[] = [];
    const jobs: unknown[] = [];
    const live: boolean[] = [];
    subscribe(seen, jobs, live);
    seen[0].emitMessage(MIXED_FRAME);
    expect(jobs).toEqual([
      [{ id: 'a' }, { id: 'b', title: 'Bedtime', status: 'running', repo: 'r' }],
    ]);
    expect(live).toEqual([true]);
  });

  it('pre-fix oracle: the old cast passed malformed rows straight through', () => {
    // Documents the hole the boundary closes: on the same frame the
    // fixed filter reduces to two rows, the old cast delivered all
    // nine — including 42, null, and the id-less object — to onJobs.
    expect(preFixParseFrameJobs(MIXED_FRAME)).toHaveLength(9);
    expect(preFixParseFrameJobs(MIXED_FRAME)).toContain(42);
  });
});
