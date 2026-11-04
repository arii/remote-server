import { describe, expect, it, vi } from 'vitest';
import {
  RUN_POLL_INTERVAL_MS,
  RUN_POLL_MAX_DELAY_MS,
  RUN_POLL_STALE_AFTER,
  createRunPollLoop,
  runPollDelayMs,
} from './useRunSubscription';
import type { JobRunProgress, PiProgressResponse } from '../apiClient';

const RID = 'abc123def456';

function runProgress(over: Partial<JobRunProgress> = {}): JobRunProgress {
  return { done: false, output: 'out', exit_code: null, action: 'logs', ...over };
}

function piProgress(over: Partial<PiProgressResponse> = {}): PiProgressResponse {
  return { done: false, output: 'pi-out', ...over };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const settle = () => new Promise((r) => setTimeout(r, 5));

function makeHarness() {
  const fetchRun = vi.fn();
  const fetchPi = vi.fn();
  const outputs: string[] = [];
  const stale: string[] = [];
  const done: string[] = [];
  const piDone: string[] = [];
  const delays: number[] = [];
  let recovered = 0;
  let nextTimer = 1;
  const pending = new Map<number, () => void>();
  const loop = createRunPollLoop({
    fetchRun,
    fetchPi,
    schedule: (fn, ms) => {
      const id = nextTimer++;
      pending.set(id, fn);
      delays.push(ms);
      return id;
    },
    cancel: (id) => {
      pending.delete(id);
    },
    onOutput: (o) => outputs.push(o),
    onDone: async (rid) => {
      done.push(rid);
      loop.stop(); // Mirrors the hook: completion stops the watch.
    },
    onPiDone: async (rid) => {
      piDone.push(rid);
      loop.stop();
    },
    onStale: (m) => stale.push(m),
    onRecovered: () => {
      recovered += 1;
    },
  });
  return {
    fetchRun,
    fetchPi,
    outputs,
    stale,
    done,
    piDone,
    delays,
    loop,
    recovered: () => recovered,
    pendingCount: () => pending.size,
    fireNext: () => {
      const entry = [...pending.entries()][0];
      if (!entry) throw new Error('no pending timer to fire');
      pending.delete(entry[0]);
      entry[1]();
    },
  };
}

describe('runPollDelayMs', () => {
  it('keeps the 2s cadence when healthy and caps exponential backoff', () => {
    expect(runPollDelayMs(0)).toBe(RUN_POLL_INTERVAL_MS);
    expect(runPollDelayMs(1)).toBe(4000);
    expect(runPollDelayMs(2)).toBe(8000);
    expect(runPollDelayMs(3)).toBe(16000);
    expect(runPollDelayMs(4)).toBe(RUN_POLL_MAX_DELAY_MS);
    expect(runPollDelayMs(10)).toBe(RUN_POLL_MAX_DELAY_MS);
  });
});

describe('run fallback poll loop (issue #496)', () => {
  it('keeps watching through a transient poll failure', async () => {
    const h = makeHarness();
    h.fetchRun
      .mockRejectedValueOnce(new Error('blip'))
      .mockResolvedValueOnce(runProgress({ output: 'half' }))
      .mockResolvedValueOnce(runProgress({ done: true, output: 'full', exit_code: 0 }));
    h.fetchPi.mockRejectedValue(new Error('no pi'));
    h.loop.start(RID);
    // First tick failed but the watch survives with backoff.
    await vi.waitFor(() => expect(h.delays).toEqual([4000]));
    expect(h.done).toEqual([]);
    expect(h.loop.consecutiveFailures()).toBe(1);
    // Next tick succeeds: output flows, cadence restored.
    h.fireNext();
    await vi.waitFor(() => expect(h.delays).toEqual([4000, 2000]));
    expect(h.outputs).toEqual(['half']);
    expect(h.loop.consecutiveFailures()).toBe(0);
    // Done finishes the watch exactly once.
    h.fireNext();
    await vi.waitFor(() => expect(h.done).toEqual([RID]));
    expect(h.outputs).toEqual(['half', 'full']);
    await settle();
    expect(h.delays).toEqual([4000, 2000]);
    expect(h.pendingCount()).toBe(0);
  });

  it('surfaces staleness after sustained failures, clears it on recovery', async () => {
    const h = makeHarness();
    h.fetchRun.mockRejectedValue(new Error('down'));
    h.fetchPi.mockRejectedValue(new Error('down'));
    h.loop.start(RID);
    for (let i = 0; i < RUN_POLL_STALE_AFTER; i++) {
      await vi.waitFor(() => expect(h.delays).toHaveLength(i + 1));
      if (i < RUN_POLL_STALE_AFTER - 1) {
        expect(h.stale).toEqual([]);
        h.fireNext();
      }
    }
    expect(h.stale).toHaveLength(1);
    expect(h.stale[0]).toContain(RID);
    // Further failures do not re-notify.
    h.fireNext();
    await vi.waitFor(() => expect(h.delays).toHaveLength(RUN_POLL_STALE_AFTER + 1));
    expect(h.stale).toHaveLength(1);
    expect(h.done).toEqual([]);
    // Recovery clears the notice and restores cadence.
    h.fetchRun.mockResolvedValueOnce(runProgress({ output: 'back' }));
    h.fireNext();
    await vi.waitFor(() => expect(h.outputs).toEqual(['back']));
    expect(h.recovered()).toBe(1);
    expect(h.delays[h.delays.length - 1]).toBe(RUN_POLL_INTERVAL_MS);
    expect(h.loop.consecutiveFailures()).toBe(0);
    h.loop.stop();
  });

  it('backs off exponentially under consecutive failures', async () => {
    const h = makeHarness();
    h.fetchRun.mockRejectedValue(new Error('down'));
    h.fetchPi.mockRejectedValue(new Error('down'));
    const expected = [4000, 8000, 16000, 30000, 30000];
    h.loop.start(RID);
    for (let i = 0; i < expected.length; i++) {
      await vi.waitFor(() => expect(h.delays).toHaveLength(i + 1));
      expect(h.delays[i]).toBe(expected[i]);
      if (i < expected.length - 1) h.fireNext();
    }
    h.loop.stop();
  });

  it('never overlaps polls: re-entrant ticks are skipped', async () => {
    const h = makeHarness();
    const gate = deferred<JobRunProgress>();
    h.fetchRun.mockReturnValueOnce(gate.promise);
    h.fetchPi.mockRejectedValue(new Error('no pi'));
    const first = h.loop.pollOnce(RID);
    const second = h.loop.pollOnce(RID);
    await second;
    expect(h.fetchRun).toHaveBeenCalledTimes(1);
    gate.resolve(runProgress({ output: 'slow' }));
    await first;
    expect(h.outputs).toEqual(['slow']);
    expect(h.fetchRun).toHaveBeenCalledTimes(1);
  });

  it('ignores a second start while the watch is active', async () => {
    const h = makeHarness();
    const gate = deferred<JobRunProgress>();
    h.fetchRun.mockReturnValue(gate.promise);
    h.fetchPi.mockRejectedValue(new Error('no pi'));
    h.loop.start(RID);
    h.loop.start(RID);
    expect(h.fetchRun).toHaveBeenCalledTimes(1);
    gate.resolve(runProgress({ output: 'one' }));
    await vi.waitFor(() => expect(h.delays).toHaveLength(1));
    expect(h.fetchRun).toHaveBeenCalledTimes(1);
    h.loop.stop();
  });

  it('stop prevents a zombie reschedule', async () => {
    const h = makeHarness();
    const gate = deferred<JobRunProgress>();
    h.fetchRun.mockReturnValueOnce(gate.promise);
    h.loop.start(RID);
    expect(h.fetchRun).toHaveBeenCalledTimes(1);
    h.loop.stop();
    gate.resolve(runProgress({ output: 'late' }));
    await vi.waitFor(() => expect(h.outputs).toEqual(['late']));
    await settle();
    expect(h.delays).toEqual([]);
    expect(h.pendingCount()).toBe(0);
  });

  it('keeps watching through pi fallback; pi-done finishes the watch', async () => {
    const h = makeHarness();
    h.fetchRun.mockRejectedValue(new Error('404'));
    h.fetchPi
      .mockResolvedValueOnce(piProgress({ output: 'pi-half' }))
      .mockResolvedValueOnce(piProgress({ done: true, output: 'pi-full' }));
    h.loop.start(RID);
    await vi.waitFor(() => expect(h.outputs).toEqual(['pi-half']));
    expect(h.piDone).toEqual([]);
    expect(h.loop.consecutiveFailures()).toBe(0);
    await vi.waitFor(() => expect(h.delays).toEqual([2000]));
    h.fireNext();
    await vi.waitFor(() => expect(h.piDone).toEqual([RID]));
    expect(h.outputs).toEqual(['pi-half', 'pi-full']);
    await settle();
    expect(h.delays).toEqual([2000]);
    expect(h.pendingCount()).toBe(0);
  });
});
