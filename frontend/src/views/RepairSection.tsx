import { useEffect, useRef, useState } from 'react';
import { dispatchRepair, fetchIssues, fetchRepairStatus } from '../apiClient';
import type { RepairStatusResponse } from '../apiClient';
import type { IssueRow } from '../reviewTypes';
import { useMock } from './mockViews';
import { repairCandidates, repairFailureMessage } from './jobsUtils';

const IDLE_REPAIR: RepairStatusResponse = {
  status: 'idle',
  timestamp: null,
  log_buffer: '',
  issue: null,
  exit_code: null,
};

export default function RepairSection({ repo }: { repo: string }) {
  const [issues, setIssues] = useState<IssueRow[] | null>(null);
  const [picked, setPicked] = useState('');
  const [manual, setManual] = useState('');
  const [status, setStatus] = useState<RepairStatusResponse>(IDLE_REPAIR);
  const [error, setError] = useState<string | null>(null);
  const timerRef = useRef<number | null>(null);

  function stopWatch() {
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }

  useEffect(() => stopWatch, []);

  useEffect(() => {
    let cancelled = false;
    if (useMock()) return;
    (async () => {
      try {
        const live = await fetchIssues(repo);
        if (!cancelled) setIssues(live.issues);
      } catch {
        if (!cancelled) setIssues([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [repo]);

  async function refresh() {
    try {
      const next = await fetchRepairStatus();
      setStatus(next);
      if (next.status === 'done' || next.status === 'failed') {
        stopWatch();
        setError(repairFailureMessage(next.status, next.issue, next.exit_code));
      }
    } catch {
      stopWatch();
    }
  }

  async function onDispatchRepair() {
    const target = (picked || manual).trim();
    if (!target) return;
    setError(null);
    try {
      if (useMock()) {
        setStatus({ ...IDLE_REPAIR, status: 'running', issue: Number(target) || null });
        return;
      }
      const started = await dispatchRepair(target);
      setStatus({ ...IDLE_REPAIR, status: started.status, issue: started.issue });
      stopWatch();
      timerRef.current = window.setInterval(() => void refresh(), 3000);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <section data-testid="repair-section" aria-label="Repair worker">
      <h3>Repair worker</h3>
      <p className="row-meta">Pick an open issue to trigger a repair run.</p>
      <div className="actions">
        <label className="row-meta" htmlFor="repair-pick">
          Issue{' '}
          <select
            id="repair-pick"
            data-testid="repair-picker"
            value={picked}
            onChange={(e) => {
              setPicked(e.target.value);
              setManual('');
            }}
          >
            <option value="">Select…</option>
            {repairCandidates(issues ?? []).map((i) => (
              <option key={i.number} value={String(i.number)}>
                #{i.number} {i.title}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          data-testid="repair-dispatch"
          disabled={!picked.trim() && !manual.trim()}
          onClick={() => void onDispatchRepair()}
        >
          Dispatch repair
        </button>
      </div>
      {issues && repairCandidates(issues).length === 0 && (
        <p className="row-meta" data-testid="repair-empty">
          {issues.length === 0
            ? 'No open issues to repair.'
            : 'Open issues are all blocked or scheduled — nothing actionable.'}
        </p>
      )}
      <details>
        <summary className="row-meta">Other issue number or URL</summary>
        <div className="actions">
          <label className="row-meta" htmlFor="repair-manual">
            Issue{' '}
            <input
              id="repair-manual"
              data-testid="repair-issue"
              value={manual}
              onChange={(e) => {
                setManual(e.target.value);
                setPicked('');
              }}
              placeholder="42 or issue URL"
            />
          </label>
        </div>
      </details>
      {error && (
        <p className="row-meta" role="alert">
          Repair failed: {error}
        </p>
      )}
      <p className="row-meta" data-testid="repair-status">
        Status: {status.status}
        {status.issue !== null ? ` · #${status.issue}` : ''}
        {status.exit_code !== null ? ` · exit ${status.exit_code}` : ''}
      </p>
      {status.log_buffer && <pre className="row-meta">{status.log_buffer}</pre>}
    </section>
  );
}
