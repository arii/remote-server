import { useEffect, useRef } from 'react';
import { fetchJobRun, fetchPiProgress } from '../apiClient';
import { completionOutcome } from './jobsUtils';

// Polls one run to completion: streams output into the progress panel,
// then posts the result notice (and plan card). Timer state lives here
// so the view stays a wiring layer. All callbacks are stable setState
// functions, so the interval closure never goes stale.
export function useRunPolling({
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
  const labelRef = useRef('');
  const repoRef = useRef('');

  function stopPoll() {
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }

  useEffect(() => stopPoll, []);

  async function pollOnce(rid: string) {
    try {
      const r = await fetchJobRun(rid);
      setTail(r.output);
      if (!r.done) return;
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
      stopPoll();
      await reload();
    } catch {
      try {
        const p = await fetchPiProgress(rid);
        setTail(p.output);
        if (!p.done) return;
      } catch {
        // No progress entry at all; fall back to job list refresh.
      }
      setRunning(null);
      stopPoll();
      await reload();
    }
  }

  function startPolling(rid: string, label: string, repo: string) {
    stopPoll();
    labelRef.current = label;
    repoRef.current = repo;
    setRunning(label);
    void pollOnce(rid);
    pollRef.current = window.setInterval(() => void pollOnce(rid), 2000);
  }

  return { startPolling };
}
