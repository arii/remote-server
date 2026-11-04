import { useEffect, useRef } from 'react';
import {
  fetchJobRun,
  fetchPiProgress,
  invalidateViewCaches,
  subscribeRunPush,
} from '../apiClient';
import type { JobRunProgress } from '../apiClient';
import { completionOutcome } from './jobsUtils';

// Tracks one run to completion: per-run SSE stream first, the
// legacy 2s snapshot poll as silent fallback when the channel is
// unavailable or dies mid-run. Completion handling (notice, plan
// card, reload) is identical on both paths. Timer/subscription
// state lives here so the view stays a wiring layer.
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
  const pollRef = useRef<number | null>(null);
  const unsubRef = useRef<(() => void) | null>(null);
  const labelRef = useRef('');
  const repoRef = useRef('');

  function stop() {
    if (unsubRef.current !== null) {
      unsubRef.current();
      unsubRef.current = null;
    }
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }

  useEffect(() => stop, []);

  async function complete(rid: string, r: JobRunProgress) {
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

  async function pollOnce(rid: string) {
    try {
      const r = await fetchJobRun(rid);
      setTail(r.output);
      if (!r.done) return;
      await complete(rid, r);
    } catch {
      try {
        const p = await fetchPiProgress(rid);
        setTail(p.output);
        if (!p.done) return;
      } catch {
        // No progress entry at all; fall back to job list refresh.
      }
      setRunning(null);
      stop();
      invalidateViewCaches();
      await reload();
    }
  }

  function startFallbackPoll(rid: string) {
    if (pollRef.current !== null) return;
    void pollOnce(rid);
    pollRef.current = window.setInterval(() => void pollOnce(rid), 2000);
  }

  function trackRun(rid: string, label: string, repo: string) {
    stop();
    labelRef.current = label;
    repoRef.current = repo;
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
