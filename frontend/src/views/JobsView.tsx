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

export default function JobsView({ repo }: { repo: string }) {
  const [jobs, setJobs] = useState<JobRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [action, setAction] = useState(ACTIONS[0]);
  const [runId, setRunId] = useState<string | null>(null);
  const [tail, setTail] = useState('');
  const [notice, setNotice] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      if (useMock()) {
        setJobs(loadJobs().jobs);
      } else {
        const live = await fetchJobs();
        setJobs(live.jobs);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  function stopPoll() {
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }

  useEffect(() => stopPoll, []);

  async function pollOnce(rid: string) {
    try {
      const p = await fetchPiProgress(rid);
      setTail(p.output);
      if (p.done) {
        stopPoll();
        void load();
      }
    } catch {
      // No pi-progress entry for plain job runs; fall back to job list refresh.
      stopPoll();
      void load();
    }
  }

  async function onDispatch() {
    setError(null);
    setNotice(null);
    try {
      const started = await startJobRun(action, repo);
      setRunId(started.id);
      setNotice(`Started ${action}: ${started.id}`);
      if (useMock()) return;
      if (/^[0-9a-f]{12}$/.test(started.id)) {
        stopPoll();
        void pollOnce(started.id);
        pollRef.current = window.setInterval(() => void pollOnce(started.id), 2000);
      } else {
        void load();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function onClear(all: boolean) {
    setError(null);
    try {
      const r = await clearJobs(all);
      setNotice(`Cleared ${r.cleared} job(s).`);
      void load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function onTakeover() {
    setError(null);
    try {
      await requestTakeover();
      setNotice('Takeover requested.');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
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
        <button type="button" data-testid="jobs-refresh" onClick={() => void load()}>
          Refresh
        </button>
      </header>
      {notice && (
        <p className="row-meta" data-testid="jobs-notice">
          {notice}
        </p>
      )}
      <div className="actions">
        <label className="row-meta" htmlFor="jobs-action">
          Action{' '}
          <select
            id="jobs-action"
            data-testid="jobs-action"
            value={action}
            onChange={(e) => setAction(e.target.value)}
          >
            {ACTIONS.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
        </label>
        <button type="button" data-testid="jobs-dispatch" onClick={() => void onDispatch()}>
          Dispatch
        </button>
        <HoldToConfirm label="Clear jobs" onFire={() => void onClear(false)} />
        <HoldToConfirm label="Clear all" onFire={() => void onClear(true)} />
        <HoldToConfirm label="Take over terminal" onFire={() => void onTakeover()} />
      </div>
      {jobs.length === 0 ? (
        <div data-testid="jobs-empty">
          <h3>No approved jobs</h3>
          <p className="row-meta">Dispatch one above or check back later.</p>
        </div>
      ) : (
        <ul className="session-list" data-testid="jobs-list">
          {jobs.map((j) => (
            <li key={j.id} className="detail" data-testid={`job-${j.id}`}>
              <span className="row-title">{labelOf(j)}</span>
              <p className="row-meta">
                {j.id} · {j.repo || repo} · {j.done ? 'done' : 'active'}
                {typeof j.exit_code === 'number' ? ` · exit ${j.exit_code}` : ''}
              </p>
              {(j.error || j.detail) && <p>{j.error || j.detail}</p>}
            </li>
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
