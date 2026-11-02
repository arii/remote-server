import { useCallback, useEffect, useRef, useState } from 'react';
import HoldToConfirm from '../components/HoldToConfirm';
import {
  clearJobs,
  fetchJobs,
  fetchPiProgress,
  requestTakeover,
  startJobRun,
} from '../apiClient';
import type { JobRecord } from '../apiTypes';
import { loadJobs, useMock } from './mockViews';

const ACTIONS = [
  'health',
  'status',
  'logs',
  'briefing-td',
  'preflight',
  'run-td',
  'merge-approved',
  'sync-agents',
  'nudge-agents',
  'warm-context',
];

function labelOf(j: JobRecord): string {
  return j.title || j.name || j.kind || j.id;
}

function JobListItem({ job, repo }: { job: JobRecord; repo: string }) {
  return (
    <li key={job.id} className="detail" data-testid={`job-${job.id}`}>
      <span className="row-title">{labelOf(job)}</span>
      <p className="row-meta">
        {job.id} · {job.repo || repo} · {job.done ? 'done' : 'active'}
        {typeof job.exit_code === 'number' ? ` · exit ${job.exit_code}` : ''}
      </p>
      {(job.error || job.detail) && <p>{job.error || job.detail}</p>}
    </li>
  );
}

interface DispatchBarProps {
  action: string;
  onAction: (action: string) => void;
  onDispatch: () => void;
  onClear: (all: boolean) => void;
  onTakeover: () => void;
}

function DispatchBar({ action, onAction, onDispatch, onClear, onTakeover }: DispatchBarProps) {
  return (
    <div className="actions">
      <label className="row-meta" htmlFor="jobs-action">
        Action{' '}
        <select
          id="jobs-action"
          data-testid="jobs-action"
          value={action}
          onChange={(e) => onAction(e.target.value)}
        >
          {ACTIONS.map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </select>
      </label>
      <button type="button" data-testid="jobs-dispatch" onClick={onDispatch}>
        Dispatch
      </button>
      <HoldToConfirm label="Clear jobs" onFire={() => onClear(false)} />
      <HoldToConfirm label="Clear all" onFire={() => onClear(true)} />
      <HoldToConfirm label="Take over terminal" onFire={onTakeover} />
    </div>
  );
}

export default function JobsView({ repo }: { repo: string }) {
  const [jobs, setJobs] = useState<JobRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [action, setAction] = useState(ACTIONS[0]);
  const [runId, setRunId] = useState<string | null>(null);
  const [tail, setTail] = useState('');
  const [notice, setNotice] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);

  async function guarded(fn: () => Promise<void>) {
    setError(null);
    try {
      await fn();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  const load = useCallback(async () => {
    if (useMock()) {
      setJobs(loadJobs().jobs);
      return;
    }
    const live = await fetchJobs();
    setJobs(live.jobs);
  }, []);

  useEffect(() => {
    void guarded(load);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load]);

  function stopPoll() {
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }

  useEffect(() => stopPoll, []);

  function startPolling(rid: string) {
    stopPoll();
    void pollOnce(rid);
    pollRef.current = window.setInterval(() => void pollOnce(rid), 2000);
  }

  async function pollOnce(rid: string) {
    try {
      const p = await fetchPiProgress(rid);
      setTail(p.output);
      if (!p.done) return;
      stopPoll();
      await load();
    } catch {
      // No pi-progress entry for plain job runs; fall back to job list refresh.
      stopPoll();
      await load();
    }
  }

  async function onDispatch() {
    setNotice(null);
    const started = await startJobRun(action, repo);
    setRunId(started.id);
    setNotice(`Started ${action}: ${started.id}`);
    if (useMock()) return;
    if (/^[0-9a-f]{12}$/.test(started.id)) startPolling(started.id);
    else await load();
  }

  async function onClear(all: boolean) {
    const r = await clearJobs(all);
    setNotice(`Cleared ${r.cleared} job(s).`);
    await load();
  }

  async function onTakeover() {
    await requestTakeover();
    setNotice('Takeover requested.');
  }

  if (error) {
    return (
      <section data-testid="jobs-error" role="alert">
        Couldn&apos;t load jobs: {error}
      </section>
    );
  }
  if (!jobs) return <section data-testid="jobs-loading">Loading…</section>;

  return (
    <section data-testid="jobs-view">
      <header className="page-head">
        <div>
          <h2>Jobs</h2>
          <p className="row-meta">{jobs.length} approved job(s)</p>
        </div>
        <button type="button" data-testid="jobs-refresh" onClick={() => void guarded(load)}>
          Refresh
        </button>
      </header>
      {notice && (
        <p className="row-meta" data-testid="jobs-notice">
          {notice}
        </p>
      )}
      <DispatchBar
        action={action}
        onAction={setAction}
        onDispatch={() => void guarded(onDispatch)}
        onClear={(all) => void guarded(() => onClear(all))}
        onTakeover={() => void guarded(onTakeover)}
      />
      {jobs.length === 0 ? (
        <div data-testid="jobs-empty">
          <h3>No approved jobs</h3>
          <p className="row-meta">Dispatch one above or check back later.</p>
        </div>
      ) : (
        <ul className="session-list" data-testid="jobs-list">
          {jobs.map((j) => (
            <JobListItem key={j.id} job={j} repo={repo} />
          ))}
        </ul>
      )}
      {(tail || runId) && (
        <div className="detail" data-testid="jobs-progress">
          <span className="row-title">Progress{runId ? ` · ${runId}` : ''}</span>
          <pre className="row-meta">{tail || 'Waiting for output…'}</pre>
        </div>
      )}
    </section>
  );
}
