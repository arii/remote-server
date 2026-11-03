import { useEffect, useRef, useState } from 'react';
import { abortRepair, dispatchRepair, fetchIssues, fetchRepairStatus, fetchVerifyCommand } from '../apiClient';
import type { RepairStatusResponse, RepairVerifyPreview } from '../apiClient';
import type { IssueRow } from '../reviewTypes';
import { useMock } from './mockViews';
import { repairCandidates, repairFailureMessage, repairVerifyBlocksDispatch, repairVerifyLine } from './jobsUtils';

const IDLE_REPAIR: RepairStatusResponse = {
  status: 'idle',
  timestamp: null,
  log_buffer: '',
  issue: null,
  exit_code: null,
};

export default function RepairSection({
  repo,
  initialStatus,
  initialPreview,
}: {
  repo: string;
  initialStatus?: RepairStatusResponse;
  initialPreview?: RepairVerifyPreview | null;
}) {
  const [issues, setIssues] = useState<IssueRow[] | null>(null);
  const [picked, setPicked] = useState('');
  const [manual, setManual] = useState('');
  const [status, setStatus] = useState<RepairStatusResponse>(
    initialStatus ?? IDLE_REPAIR,
  );
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<RepairVerifyPreview | null>(
    initialPreview ?? null,
  );
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

  const target = (picked || manual).trim();
  useEffect(() => {
    let cancelled = false;
    if (useMock()) return;
    if (!target) {
      setPreview(null);
      return;
    }
    setPreview(null);
    (async () => {
      try {
        const live = await fetchVerifyCommand(target);
        if (!cancelled) setPreview(live);
      } catch {
        if (!cancelled) setPreview(null);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [picked, manual]);

  async function refresh() {
    try {
      const next = await fetchRepairStatus();
      setStatus(next);
      if (next.status === 'done' || next.status === 'failed' || next.status === 'aborted') {
        stopWatch();
        setError(repairFailureMessage(next.status, next.issue, next.exit_code));
      }
    } catch {
      stopWatch();
    }
  }

  async function onAbortRepair() {
    stopWatch();
    setError(null);
    try {
      if (useMock()) {
        setStatus({ ...IDLE_REPAIR, status: 'aborted', issue: status.issue });
        return;
      }
      await abortRepair();
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
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
          disabled={!target || repairVerifyBlocksDispatch(preview)}
          onClick={() => void onDispatchRepair()}
        >
          Dispatch repair
        </button>
        {status.status === 'running' && (
          <button type="button" data-testid="repair-abort" onClick={() => void onAbortRepair()}>
            Abort
          </button>
        )}
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
      {repairVerifyLine(preview) && (
        <p className="row-meta" data-testid="repair-verify">
          {repairVerifyLine(preview)}
        </p>
      )}
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
