import { useEffect, useRef } from 'react';
import {
  fetchJobRun,
  fetchPiProgress,
  invalidateViewCaches,
  subscribeRunPush,
} from '../apiClient';
import type { JobRunProgress, PiProgressResponse } from '../apiClient';
import { completionOutcome } from './jobsUtils';

// Tracks one run to completion: per-run SSE stream first, the
// legacy 2s snapshot poll as silent fallback when the channel is
// unavailable or dies mid-run. Completion handling (notice, plan
// card, reload) is identical on both paths. Timer/subscription
// state lives here so the view stays a wiring layer.

// Fallback poll tuning: the 2s cadence is unchanged; under
// failure the loop backs off exponentially (capped) and reports
// staleness after N consecutive errors. The watch stops only on
// done or explicit stop — never on a transient failure (#496).
export const RUN_POLL_INTERVAL_MS = 2000;
export const RUN_POLL_MAX_DELAY_MS = 30000;
export const RUN_POLL_STALE_AFTER = 3;

export function runPollDelayMs(consecutiveFailures: number): number {
  return Math.min(RUN_POLL_INTERVAL_MS * 2 ** consecutiveFailures, RUN_POLL_MAX_DELAY_MS);
}

export interface RunPollLoop {
  start: (rid: string) => void;
  stop: () => void;
  pollOnce: (rid: string) => Promise<void>;
  consecutiveFailures: () => number;
}

// React-free fallback poll loop: a single timeout chain (ticks
// never overlap by construction) plus an in-flight guard (a slow
// poll is never doubled by a re-entrant tick). Completion owns
// stopping: onDone/onPiDone must stop the loop, so a stale done
// for a superseded run can be dropped without killing the fresh
// watch. Timer-neutral so vitest can drive it without a DOM.
export function createRunPollLoop(deps: {
  fetchRun: (rid: string) => Promise<JobRunProgress>;
  fetchPi: (rid: string) => Promise<PiProgressResponse>;
  schedule: (fn: () => void, ms: number) => number;
  cancel: (id: number) => void;
  onOutput: (output: string) => void;
  onDone: (rid: string, r: JobRunProgress) => Promise<void>;
  onPiDone: (rid: string) => Promise<void>;
  onStale: (message: string) => void;
  onRecovered: () => void;
}): RunPollLoop {
  let timer: number | null = null;
  let active = false;
  let generation = 0;
  let inflight = false;
  let failures = 0;
  let stale = false;

  function stop() {
    active = false;
    generation += 1;
    if (timer !== null) {
      deps.cancel(timer);
      timer = null;
    }
  }

  function markHealthy() {
    failures = 0;
    if (stale) {
      stale = false;
      deps.onRecovered();
    }
  }

  async function pollOnce(rid: string) {
    if (inflight) return;
    inflight = true;
    try {
      const r = await deps.fetchRun(rid);
      markHealthy();
      deps.onOutput(r.output);
      if (r.done) await deps.onDone(rid, r);
    } catch {
      try {
        const p = await deps.fetchPi(rid);
        markHealthy();
        deps.onOutput(p.output);
        if (p.done) await deps.onPiDone(rid);
        return;
      } catch {
        // Transient failure (network blip, registry restart):
        // keep watching with backoff instead of abandoning.
      }
      failures += 1;
      if (failures >= RUN_POLL_STALE_AFTER && !stale) {
        stale = true;
        deps.onStale(`Run ${rid} stopped responding — still watching.`);
      }
    } finally {
      inflight = false;
    }
  }

  function start(rid: string) {
    if (active) return;
    active = true;
    const gen = generation;
    const tick = async () => {
      timer = null;
      await pollOnce(rid);
      if (!active || gen !== generation) return;
      timer = deps.schedule(() => void tick(), runPollDelayMs(failures));
    };
    void tick();
  }

  return { start, stop, pollOnce, consecutiveFailures: () => failures };
}

export function useRunSubscription({
  setTail,
  setRunning,
  setNotice,
  setPlanCard,
  reload,
}: {
  setTail: (tail: string) => void;
  setRunning: (label: string | null) => void;
  setNotice: (notice: string) => void;
  setPlanCard: (card: { repo: string; name: string }) => void;
  reload: () => Promise<void>;
}) {
  const unsubRef = useRef<(() => void) | null>(null);
  const loopRef = useRef<RunPollLoop | null>(null);
  const labelRef = useRef('');
  const repoRef = useRef('');
  const ridRef = useRef<string | null>(null);

  function stop() {
    ridRef.current = null;
    if (unsubRef.current !== null) {
      unsubRef.current();
      unsubRef.current = null;
    }
    loopRef.current?.stop();
  }

  useEffect(() => stop, []);

  async function complete(rid: string, r: JobRunProgress) {
    if (rid !== ridRef.current) return;
    setRunning(null);
    const outcome = completionOutcome({
      runId: rid,
      action: r.action,
      output: r.output,
      exitCode: r.exit_code,
      label: labelRef.current,
      repo: repoRef.current,
    });
    setNotice(outcome.notice);
    if (outcome.planCard) setPlanCard(outcome.planCard);
    stop();
    invalidateViewCaches();
    await reload();
  }

  function ensureLoop(): RunPollLoop {
    if (loopRef.current === null) {
      loopRef.current = createRunPollLoop({
        fetchRun: fetchJobRun,
        fetchPi: fetchPiProgress,
        schedule: (fn, ms) => window.setTimeout(fn, ms),
        cancel: (id) => window.clearTimeout(id),
        onOutput: setTail,
        onDone: (rid, r) => complete(rid, r),
        onPiDone: async (rid) => {
          if (rid !== ridRef.current) return;
          setRunning(null);
          stop();
          invalidateViewCaches();
          await reload();
        },
        onStale: setNotice,
        onRecovered: () => setNotice(''),
      });
    }
    return loopRef.current;
  }

  function startFallbackPoll(rid: string) {
    ensureLoop().start(rid);
  }

  function trackRun(rid: string, label: string, repo: string) {
    stop();
    labelRef.current = label;
    repoRef.current = repo;
    ridRef.current = rid;
    setRunning(label);
    unsubRef.current = subscribeRunPush(rid, {
      onProgress: (p) => setTail(p.output),
      onDone: (p) => {
        setTail(p.output);
        void complete(rid, p);
      },
      onDead: () => startFallbackPoll(rid),
    });
  }

  return { trackRun };
}
