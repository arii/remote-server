import { useEffect, useRef, useState } from 'react';
import BranchField, { useBranchField } from '../components/BranchField';
import { abortRepair, dispatchRepair, fetchIssues, fetchRepairStatus, fetchVerifyCommand, setRepairSchedule } from '../apiClient';
import type { RepairStatusResponse, RepairVerifyPreview } from '../apiClient';
import type { IssueRow } from '../reviewTypes';
import { useMock } from './mockViews';
import { filterRepairCandidates, repairCandidates, repairFailureMessage, repairVerifyBlocksDispatch, repairVerifyLine } from './jobsUtils';

const IDLE_REPAIR: RepairStatusResponse = {
  status: 'idle',
  timestamp: null,
  log_buffer: '',
  issue: null,
  exit_code: null,
  repo: null,
};

// Filterable repair-target picker. The native select stays (a11y);
// the text filter narrows its options by number/title substring.
export function RepairPicker({
  candidates,
  picked,
  filter,
  onPick,
  onFilter,
}: {
  candidates: IssueRow[];
  picked: string;
  filter: string;
  onPick: (v: string) => void;
  onFilter: (q: string) => void;
}) {
  const filtered = filterRepairCandidates(candidates, filter);
  return (
    <>
      <label className="row-meta" htmlFor="repair-filter">
        Filter{' '}
        <input
          id="repair-filter"
          data-testid="repair-filter"
          value={filter}
          onChange={(e) => onFilter(e.target.value)}
          placeholder="number or title…"
        />
      </label>
      <label className="row-meta" htmlFor="repair-pick">
        Issue{' '}
        <select
          id="repair-pick"
          data-testid="repair-picker"
          value={picked}
          onChange={(e) => onPick(e.target.value)}
        >
          <option value="">Select…</option>
          {filtered.map((i) => (
            <option key={i.number} value={String(i.number)}>
              #{i.number} {i.title}
            </option>
          ))}
        </select>
      </label>
      {filter.trim() && filtered.length === 0 && candidates.length > 0 && (
        <p className="row-meta" data-testid="repair-filter-empty">
          No candidates match this filter.
        </p>
      )}
    </>
  );
}

export default function RepairSection({
  repo,
  defaultBranch,
  initialStatus,
  initialPreview,
  initialPicked,
}: {
  repo: string;
  // Checkout branch of the viewed repo: dispatch validates the
  // branch against that repo (#435, #708).
  defaultBranch?: string;
  initialStatus?: RepairStatusResponse;
  initialPreview?: RepairVerifyPreview | null;
  initialPicked?: string;
}) {
  const [issues, setIssues] = useState<IssueRow[] | null>(null);
  const [branch, setBranch] = useBranchField(repo, defaultBranch);
  const [picked, setPicked] = useState(initialPicked ?? '');
  const [manual, setManual] = useState('');
  const [filter, setFilter] = useState('');
  const candidates = repairCandidates(issues ?? []);
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

  useEffect(() => {
    if (useMock()) return;
    void loadSchedules();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repo]);

  const target = (picked || manual).trim();
  const targetNumber = target.match(/\/issues\/(\d+)/)?.[1] ?? target.replace(/^#/, '');
  const targetSchedule = (status.schedules ?? []).find(
    (s) => String(s.issue) === targetNumber,
  );
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
        const live = await fetchVerifyCommand(target, repo);
        if (!cancelled) setPreview(live);
      } catch {
        if (!cancelled) setPreview(null);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [picked, manual, repo]);

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

  async function loadSchedules() {
    try {
      setStatus(await fetchRepairStatus());
    } catch {
      // Schedules stay hidden when status is unreachable.
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

  async function onScheduleRepair(cadence: string) {
    const target = (picked || manual).trim();
    if (!target) return;
    setError(null);
    try {
      if (useMock()) return;
      await setRepairSchedule(target, cadence);
      await loadSchedules();
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
      const started = await dispatchRepair(target, branch, repo);
      setStatus({ ...IDLE_REPAIR, status: started.status, issue: started.issue, repo: started.repo });
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
        <RepairPicker
          candidates={candidates}
          picked={picked}
          filter={filter}
          onPick={(v) => {
            setPicked(v);
            setManual('');
          }}
          onFilter={(q) => {
            setFilter(q);
            // Never dispatch a pick the filter hides: drop a
            // selection that no longer matches, keep one that does.
            setPicked((prev) =>
              prev &&
              !filterRepairCandidates(candidates, q).some(
                (c) => String(c.number) === prev,
              )
                ? ''
                : prev,
            );
          }}
        />
        <BranchField
          id="repair-branch"
          testid="repair-branch"
          value={branch}
          onChange={setBranch}
        />
        <button
          type="button"
          data-testid="repair-dispatch"
          disabled={!target || repairVerifyBlocksDispatch(preview) || status.status === 'running'}
          onClick={() => void onDispatchRepair()}
        >
          Dispatch repair
        </button>
        {status.status === 'running' && (
          <button type="button" data-testid="repair-abort" onClick={() => void onAbortRepair()}>
            Abort
          </button>
        )}
        <label className="row-meta" htmlFor="repair-schedule-cadence">
          Schedule{' '}
          <select
            id="repair-schedule-cadence"
            data-testid="repair-schedule-cadence"
            value={targetSchedule?.cadence ?? 'off'}
            disabled={!target}
            onChange={(e) => void onScheduleRepair(e.target.value)}
          >
            <option value="off">Off</option>
            <option value="daily">Daily</option>
            <option value="weekly">Weekly</option>
          </select>
        </label>
        {targetSchedule && (
          <button
            type="button"
            data-testid="repair-schedule-disable"
            onClick={() => void onScheduleRepair('off')}
          >
            Disable schedule
          </button>
        )}
      </div>
      {targetSchedule && (
        <p className="row-meta" data-testid="repair-schedule-next">
          Next run: {new Date(targetSchedule.next_run * 1000).toLocaleString()}
        </p>
      )}
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
        {status.repo ? ` · ${status.repo}` : ''}
        {status.exit_code !== null ? ` · exit ${status.exit_code}` : ''}
      </p>
      {status.log_buffer && <pre className="row-meta">{status.log_buffer}</pre>}
    </section>
  );
}
