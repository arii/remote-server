import { useCallback, useEffect, useRef, useState } from 'react';
import HoldToConfirm from '../components/HoldToConfirm';
import {
  clearJobs,
  dispatchRepair,
  fetchIssues,
  fetchJobRun,
  fetchJobs,
  fetchPiProgress,
  fetchRepairStatus,
  requestTakeover,
  startJobRun,
  startTdRun,
} from '../apiClient';
import type { RepairStatusResponse } from '../apiClient';
import type { JobRecord } from '../apiTypes';
import type { IssueRow } from '../reviewTypes';
import { loadJobs, useMock } from './mockViews';

interface FixedAction {
  action: string;
  label: string;
  desc: string;
}

const OBSERVE: FixedAction[] = [
  { action: 'health', label: 'Stack health', desc: 'Disk, memory and container snapshot.' },
  { action: 'status', label: 'Repo status', desc: 'Git branch, recent commits, images.' },
  { action: 'logs', label: 'Recent logs', desc: 'Last 100 terminal and ollama lines.' },
];

interface TdCommand {
  slug: string;
  label: string;
  desc: string;
  needsPr: boolean;
  hold: boolean;
  build: (pr: string) => string;
}

const TD_COMMANDS: TdCommand[] = [
  {
    slug: 'fix-ci',
    label: 'Fix CI failures',
    desc: 'Pushes automated CI fixes to the PR branch.',
    needsPr: true,
    hold: true,
    build: (pr) => `td --json agent fix-ci --pr ${pr} --execute`,
  },
  {
    slug: 'read-reviews',
    label: 'Read PR reviews',
    desc: 'Review verdicts and threads for the PR.',
    needsPr: true,
    hold: false,
    build: (pr) => `td gh read-pr-reviews --pr ${pr}`,
  },
  {
    slug: 'read-comments',
    label: 'Read PR comments',
    desc: 'All comments on the PR.',
    needsPr: true,
    hold: false,
    build: (pr) => `td gh read-pr-comments --pr ${pr}`,
  },
  {
    slug: 'resolve-conflicts',
    label: 'Resolve conflicts',
    desc: 'Attempts merge-conflict resolution on the PR.',
    needsPr: true,
    hold: true,
    build: (pr) => `td gh resolve-conflicts --pr ${pr}`,
  },
  {
    slug: 'list-sessions',
    label: 'Agent sessions',
    desc: 'Active background agent sessions.',
    needsPr: false,
    hold: false,
    build: () => 'td --json agent list-sessions',
  },
  {
    slug: 'feedback-check',
    label: 'Feedback check',
    desc: 'Pending agent run feedback.',
    needsPr: false,
    hold: false,
    build: () => 'td --json agent run-feedback-check',
  },
];

interface DangerAction {
  action: string;
  label: string;
  consequence: string;
}

const DANGER: DangerAction[] = [
  {
    action: 'merge-approved',
    label: 'Merge approved PRs',
    consequence: 'Merges every approved, green PR.',
  },
  {
    action: 'create-plan',
    label: 'Write briefing plan',
    consequence: 'Writes a dated plan file into .agents/plans.',
  },
  {
    action: 'deploy-buttons',
    label: 'Redeploy this console',
    consequence: 'Pulls, rebuilds and restarts the console container.',
  },
  {
    action: 'recreate',
    label: 'Restart terminal',
    consequence: 'Force-restarts the terminal container; drops live sessions.',
  },
  {
    action: 'recreate-code',
    label: 'Restart code editor',
    consequence: 'Force-restarts the code-server container.',
  },
];

function labelOf(j: JobRecord): string {
  return j.title || j.name || j.kind || j.id;
}

// The human result of a run: last meaningful output line, skipping the
// `$ ...` command echoes the engine prepends. Empty when nothing surfaced.
export function resultLine(output: string): string {
  const lines = output
    .split('\n')
    .map((l) => l.trim())
    .filter((l) => l.length > 0 && !l.startsWith('$'));
  return lines.length > 0 ? lines[lines.length - 1] : '';
}

const IDLE_REPAIR: RepairStatusResponse = {
  status: 'idle',
  timestamp: null,
  log_buffer: '',
  issue: null,
  exit_code: null,
};

function RepairSection({ repo }: { repo: string }) {
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
        setError(
          next.status === 'failed'
            ? `Repair run for #${next.issue ?? '?'} failed${next.exit_code !== null ? ` (exit ${next.exit_code})` : ''}.`
            : null,
        );
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
            {(issues ?? []).map((i) => (
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

function JobListItem({
  job,
  repo,
  onSelect,
}: {
  job: JobRecord;
  repo: string;
  onSelect: (id: string) => void;
}) {
  return (
    <li key={job.id} className="detail" data-testid={`job-${job.id}`}>
      <span className="row-title">{labelOf(job)}</span>
      <p className="row-meta">
        {job.id} · {job.repo || repo} · {job.done ? 'done' : 'active'}
        {typeof job.exit_code === 'number' ? ` · exit ${job.exit_code}` : ''}
      </p>
      {(job.error || job.detail) && <p>{job.error || job.detail}</p>}
      <div className="actions">
        <button type="button" onClick={() => onSelect(job.id)}>
          View output
        </button>
      </div>
    </li>
  );
}

export default function JobsView({ repo }: { repo: string }) {
  const [jobs, setJobs] = useState<JobRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [running, setRunning] = useState<string | null>(null);
  const [tail, setTail] = useState('');
  const [notice, setNotice] = useState<string | null>(null);
  const [pr, setPr] = useState('');
  const pollRef = useRef<number | null>(null);
  const labelRef = useRef<string>('');
  const progressRef = useRef<HTMLDivElement | null>(null);

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

  function startPolling(rid: string, label: string) {
    stopPoll();
    labelRef.current = label;
    setRunning(label);
    void pollOnce(rid);
    pollRef.current = window.setInterval(() => void pollOnce(rid), 2000);
  }

  async function pollOnce(rid: string) {
    try {
      const r = await fetchJobRun(rid);
      setTail(r.output);
      if (!r.done) return;
      setRunning(null);
      const label = labelRef.current || 'Run';
      const result = resultLine(r.output);
      if (r.exit_code !== 0 && r.exit_code !== null) {
        setNotice(`Run ${rid} failed (exit ${r.exit_code})${result ? `: ${result}` : '.'}`);
      } else {
        setNotice(result ? `Finished ${label}: ${result}` : `Finished ${label}.`);
      }
      stopPoll();
      await load();
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
      await load();
    }
  }

  async function onSelectRun(id: string) {
    const r = await fetchJobRun(id);
    setRunId(id);
    setRunning(null);
    setTail(r.output || 'No output yet.');
  }

  async function launch(label: string, start: () => Promise<{ id: string }>) {
    setNotice(null);
    setTail('');
    const started = await start();
    setRunId(started.id);
    setNotice(`Started ${label}: ${started.id}`);
    if (useMock()) return;
    if (/^[0-9a-f]{12}$/.test(started.id)) startPolling(started.id, label);
    else await load();
  }

  // Bring the progress panel into view once it renders for a new run.
  useEffect(() => {
    if (!running) return;
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    progressRef.current?.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'nearest' });
  }, [running]);

  function onDispatchFixed(a: FixedAction, confirmed: boolean) {
    return guarded(() => launch(a.label, () => startJobRun(a.action, repo, confirmed)));
  }

  function onDispatchTd(cmd: TdCommand) {
    const n = pr.trim();
    if (cmd.needsPr && !/^\d+$/.test(n)) {
      setError('Enter a PR number first.');
      return;
    }
    void guarded(() => launch(cmd.label, () => startTdRun(cmd.build(n), repo)));
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

  const prOk = /^\d+$/.test(pr.trim());

  return (
    <section data-testid="jobs-view">
      <header className="page-head">
        <div>
          <h2>Jobs</h2>
          <p className="row-meta">
            {jobs.length} approved job{jobs.length === 1 ? '' : 's'}
          </p>
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
      {(tail || runId) && (
        <div className="detail" data-testid="jobs-progress" ref={progressRef}>
          <span className="row-title">
            {running ? `Running ${running}…` : `Output${runId ? ` · ${runId}` : ''}`}
          </span>
          {!running && runId && resultLine(tail) && (
            <p className="row-meta" data-testid="jobs-result">
              {resultLine(tail)}
            </p>
          )}
          <pre className="row-meta">{tail || 'Waiting for output…'}</pre>
        </div>
      )}

      <section aria-label="Observe">
        <h3>Observe</h3>
        <div className="actions">
          {OBSERVE.map((a) => (
            <button
              key={a.action}
              type="button"
              data-testid={`dispatch-${a.action}`}
              title={a.desc}
              onClick={() => onDispatchFixed(a, false)}
            >
              {a.label}
            </button>
          ))}
        </div>
      </section>

      <section aria-label="Agent commands">
        <h3>Agent commands</h3>
        <div className="cmd-group" data-testid="td-pr-group">
          <p className="row-meta">For a PR</p>
          <div className="actions">
            <label className="row-meta" htmlFor="td-pr">
              PR number{' '}
              <input
                id="td-pr"
                data-testid="td-pr"
                inputMode="numeric"
                value={pr}
                onChange={(e) => setPr(e.target.value)}
                placeholder="123"
              />
            </label>
          </div>
          <div className="actions">
            {TD_COMMANDS.filter((c) => c.needsPr && !c.hold).map((c) => (
              <button
                key={c.slug}
                type="button"
                data-testid={`dispatch-td-${c.slug}`}
                title={c.desc}
                disabled={!prOk}
                onClick={() => onDispatchTd(c)}
              >
                {c.label}
              </button>
            ))}
          </div>
          <div className="actions">
            <span className="row-meta">Change (hold to run):</span>
            {TD_COMMANDS.filter((c) => c.needsPr && c.hold).map((c) => (
              <HoldToConfirm
                key={c.slug}
                label={c.label}
                disabled={!prOk}
                onFire={() => onDispatchTd(c)}
              />
            ))}
          </div>
        </div>
        <div className="cmd-group" data-testid="td-repo-group">
          <p className="row-meta">Whole repo</p>
          <div className="actions">
            {TD_COMMANDS.filter((c) => !c.needsPr).map((c) =>
              c.hold ? (
                <HoldToConfirm
                  key={c.slug}
                  label={c.label}
                  onFire={() => onDispatchTd(c)}
                />
              ) : (
                <button
                  key={c.slug}
                  type="button"
                  data-testid={`dispatch-td-${c.slug}`}
                  title={c.desc}
                  onClick={() => onDispatchTd(c)}
                >
                  {c.label}
                </button>
              ),
            )}
          </div>
        </div>
      </section>

      <section aria-label="Danger zone" className="danger-zone">
        <h3>Danger zone</h3>
        <ul className="session-list">
          {DANGER.map((d) => (
            <li key={d.action} className="danger-row" data-testid={`danger-${d.action}`}>
              <div>
                <span className="row-title">{d.label}</span>
                <p className="row-meta">{d.consequence}</p>
              </div>
              <HoldToConfirm
                label={d.label}
                onFire={() =>
                  void guarded(() =>
                    launch(d.label, () => startJobRun(d.action, repo, true)),
                  )
                }
              />
            </li>
          ))}
          <li className="danger-row" data-testid="danger-clear-finished">
            <div>
              <span className="row-title">Clear finished runs</span>
              <p className="row-meta">Drops finished runs from the list below.</p>
            </div>
            <HoldToConfirm label="Clear finished" onFire={() => void guarded(() => onClear(false))} />
          </li>
          <li className="danger-row" data-testid="danger-clear-all">
            <div>
              <span className="row-title">Clear everything</span>
              <p className="row-meta">Drops all runs, including running ones.</p>
            </div>
            <HoldToConfirm label="Clear everything" onFire={() => void guarded(() => onClear(true))} />
          </li>
          <li className="danger-row" data-testid="danger-takeover">
            <div>
              <span className="row-title">Take over terminal</span>
              <p className="row-meta">Disconnects other viewers of the shared terminal.</p>
            </div>
            <HoldToConfirm label="Take over terminal" onFire={() => void guarded(onTakeover)} />
          </li>
        </ul>
      </section>

      {jobs.length === 0 ? (
        <div data-testid="jobs-empty">
          <h3>No approved jobs</h3>
          <p className="row-meta">Dispatch one above or check back later.</p>
        </div>
      ) : (
        <ul className="session-list" data-testid="jobs-list">
          {jobs.map((j) => (
            <JobListItem
              key={j.id}
              job={j}
              repo={repo}
              onSelect={(id) => void guarded(() => onSelectRun(id))}
            />
          ))}
        </ul>
      )}
      <RepairSection repo={repo} />
    </section>
  );
}
