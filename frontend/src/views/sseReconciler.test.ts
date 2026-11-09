import { describe, expect, it } from 'vitest';
import { createSseReconciler, pumpSse, type SseEvent } from './sse';

// Bounded SSE replay (issue #1208): the backend stamps every frame
// with a process-wide monotonic `seq`; the shared pump and both
// token pumps drop frames at or behind the last applied seq so a
// socket drop + reconnect can neither re-render nor double-apply.
// The seen window is bounded (default 200) so a replay burst can
// neither grow memory nor re-render. No-seq frames (old servers,
// heartbeats) always pass.

function frame(name: string, seq?: number | string): SseEvent {
  const payload = seq === undefined ? { token: 'x' } : { token: 'x', seq };
  return { name, data: JSON.stringify(payload) };
}

function chunkReader(
  chunks: Uint8Array[],
): ReadableStreamDefaultReader<Uint8Array> {
  const queue = [...chunks];
  return {
    read: async () => {
      const value = queue.shift();
      if (!value) return { done: true, value: undefined };
      return { done: false, value };
    },
  } as unknown as ReadableStreamDefaultReader<Uint8Array>;
}

const enc = (s: string): Uint8Array => new TextEncoder().encode(s);

describe('createSseReconciler (issue #1208)', () => {
  it('passes the first seq and tracks it as the high-water mark', () => {
    const r = createSseReconciler();
    expect(r.fresh(frame('token', 1))).toBe(true);
    expect(r.last()).toBe(1);
  });

  it('drops stale and redelivered frames, keeps newer ones', () => {
    const r = createSseReconciler();
    expect(r.fresh(frame('token', 10))).toBe(true);
    expect(r.fresh(frame('token', 11))).toBe(true);
    // Exact redelivery (reconnect replay of the applied prefix).
    expect(r.fresh(frame('token', 11))).toBe(false);
    expect(r.fresh(frame('token', 10))).toBe(false);
    // Older than the high-water mark.
    expect(r.fresh(frame('token', 3))).toBe(false);
    // Newer still applies and moves the mark.
    expect(r.fresh(frame('token', 12))).toBe(true);
    expect(r.last()).toBe(12);
  });

  it('passes no-seq frames for backward compatibility', () => {
    const r = createSseReconciler();
    expect(r.fresh(frame('token', 7))).toBe(true);
    expect(r.fresh(frame('token'))).toBe(true);
    expect(r.fresh({ name: 'token', data: 'not-json' })).toBe(true);
    expect(r.fresh({ name: 'token', data: '{"seq":"9"}' })).toBe(true);
    // Non-numeric seqs never move the high-water mark.
    expect(r.last()).toBe(7);
  });

  it('reset clears the high-water mark for a new stream', () => {
    const r = createSseReconciler();
    expect(r.fresh(frame('token', 5))).toBe(true);
    r.reset();
    expect(r.last()).toBeNull();
    expect(r.fresh(frame('token', 5))).toBe(true);
  });

  it('stays correct under a burst past the 200-frame window', () => {
    const r = createSseReconciler();
    for (let seq = 1; seq <= 500; seq++) {
      expect(r.fresh(frame('token', seq))).toBe(true);
    }
    expect(r.last()).toBe(500);
    // Full-burst replay after a reconnect: everything applied.
    for (let seq = 1; seq <= 500; seq++) {
      expect(r.fresh(frame('token', seq))).toBe(false);
    }
    expect(r.fresh(frame('token', 501))).toBe(true);
  });

  it('keeps drop semantics with a small window', () => {
    const r = createSseReconciler(5);
    for (let seq = 1; seq <= 50; seq++) {
      expect(r.fresh(frame('token', seq))).toBe(true);
    }
    expect(r.fresh(frame('token', 50))).toBe(false);
    expect(r.fresh(frame('token', 1))).toBe(false);
    expect(r.fresh(frame('token', 51))).toBe(true);
  });
});

describe('pumpSse freshness gate (issue #1208)', () => {
  it('skips stale token frames but always delivers errors', async () => {
    const reconciler = createSseReconciler();
    const seen: SseEvent[] = [];
    const bytes =
      `event: token\ndata: {"token":"a","seq":1}\n\n` +
      `event: token\ndata: {"token":"a-replay","seq":1}\n\n` +
      `event: error\ndata: {"error":"boom","seq":1}\n\n` +
      `event: token\ndata: {"token":"b","seq":2}\n\n`;
    await pumpSse(chunkReader([enc(bytes)]), (ev) => seen.push(ev), {
      isFresh: (ev) => reconciler.fresh(ev),
    });
    // Live seq 1, stale error redelivery still delivered (terminal
    // information is never silently dropped), live seq 2.
    expect(seen.map((ev) => ev.name)).toEqual(['token', 'error', 'token']);
    expect(seen[0].data).toContain('"a"');
    expect(seen[1].data).toContain('boom');
    expect(seen[2].data).toContain('"b"');
  });
});
