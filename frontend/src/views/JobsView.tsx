import { useCallback, useEffect, useRef, useState } from 'react';
import TriageHint from '../components/TriageHint';
import {
  clearJobs,
  fetchJobRun,
  fetchJobs,
  fetchPlan,
  requestTakeover,
  startJobRun,
  startTdRun,
} from '../apiClient';
import type { JobRecord } from '../apiTypes';
import { useTriageKeys } from '../triageKeys';
import { loadJobs, useMock } from './mockViews';
import {
  attachIdFromConflict,
  conflictBlockReason,
  fixCiBlockReason,
  jobsListActive,
  planExecuteSeed,
  resultLine,
} from './jobsUtils';
import DangerZone from './DangerZone';
import RepairSection from './RepairSection';
import TdCommands from './TdCommands';
import type { TdCommand } from './TdCommands';
import { useMergeReady, usePrRefState } from './useJobsData';
import { useRunPolling } from './useRunPolling';

function labelOf(j: JobRecord): string {
  return j.title || j.name || j.kind || j.id;
}

function JobListItem({
  job,
  repo,
  kbdFocus,
  onSelect,
}: {
  job: JobRecord;
  repo: string;
  kbdFocus?: boolean;
  onSelect: (id: string) => void;
}) {
  return (
    <li
      key={job.id}
      className={`detail${kbdFocus ? ' kbd-focus' : ''}`}
      data-testid={`job-${job.id}`}
    >
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

function JobsHeader({
  list,
  running,
  onRefresh,
}: {
  list: JobRecord[];
  running: string | null;
  onRefresh: () => void;
}) {
  return (
    <header className="page-head">
      <div>
        <h3>Jobs</h3>
        <p className="row-meta">
          {list.length} run{list.length === 1 ? '' : 's'}
          {jobsListActive(list) || running ? ' · Live' : ''}
        </p>
      </div>
      <button type="button" data-testid="jobs-refresh" onClick={onRefresh}>
        Refresh
      </button>
    </header>
  );
}

function RunProgressPanel({
  tail,
  runId,
  running,
  panelRef,
}: {
  tail: string;
  runId: string | null;
  running: string | null;
  panelRef: React.RefObject<HTMLDivElement | null>;
}) {
  if (!tail && !runId) return null;
  return (
    <div className="detail" data-testid="jobs-progress" ref={panelRef}>
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
  );
}

function PlanCardPanel({
  card,
  open,
  content,
  onToggle,
  onExecute,
}: {
  card: { repo: string; name: string } | null;
  open: boolean;
  content: string | null;
  onToggle: () => void;
  onExecute: () => void;
}) {
  if (!card) return null;
  return (
    <div className="detail" data-testid="jobs-plan">
      <span className="row-title">Plan written</span>
      <p className="row-meta">
        {card.repo}/.agents/plans/{card.name}
      </p>
      <div className="actions">
        <button type="button" data-testid="jobs-plan-toggle" onClick={onToggle}>
          {open ? 'Hide plan' : 'View plan'}
        </button>{' '}
        <button type="button" data-testid="jobs-plan-execute" onClick={onExecute}>
          Execute plan
        </button>
      </div>
      {open && <pre className="row-meta">{content ?? 'Loading…'}</pre>}
    </div>
  );
}

function JobsListPanel({
  list,
  repo,
  cursor,
  onSelect,
}: {
  list: JobRecord[];
  repo: string;
  cursor: number | null;
  onSelect: (id: string) => void;
}) {
  if (list.length === 0) {
    return (
      <div data-testid="jobs-empty">
        <h3>No batch runs</h3>
        <p className="row-meta">
          Dispatch one above. Agent chat runs live under Chat → Past runs.
        </p>
      </div>
    );
  }
  return (
    <ul className="session-list" data-testid="jobs-list">
      {list.map((j, ji) => (
        <JobListItem
          key={j.id}
          job={j}
          repo={repo}
          kbdFocus={cursor === ji}
          onSelect={onSelect}
        />
      ))}
    </ul>
  );
}

export default function JobsView({
  repo,
  triageSuspended = false,
  onAskAgent,
}: {
  repo: string;
  // Drawer-open flag: while true (agent drawer / PTY terminal open),
  // triage keys are disabled.
  triageSuspended?: boolean;
  onAskAgent: (seed: string) => void;
}) {
  const [jobs, setJobs] = useState<JobRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [running, setRunning] = useState<string | null>(null);
  const [tail, setTail] = useState('');
  const [notice, setNotice] = useState<string | null>(null);
  const [pr, setPr] = useState('');
  // Relevance state. null = unknown (fail open: buttons stay enabled).
  const mergeReady = useMergeReady(repo);
  const prState = usePrRefState(pr, repo);
  const [planCard, setPlanCard] = useState<{ repo: string; name: string } | null>(null);
  const [planOpen, setPlanOpen] = useState(false);
  const [planContent, setPlanContent] = useState<string | null>(null);
  const progressRef = useRef<HTMLDivElement | null>(null);
  // Live list for the triage opener (hook subscribes once, reads via ref).
  const jobsRef = useRef(jobs);
  jobsRef.current = jobs;
  const [cursor] = useTriageKeys({
    count: jobs?.length ?? 0,
    suspended: triageSuspended,
    onOpen: (i) => {
      const j = jobsRef.current?.[i];
      if (j) void guarded(() => onSelectRun(j.id));
    },
    rowTestId: (i) => `job-${jobsRef.current?.[i]?.id ?? ''}`,
  });

  async function tryLoadPlan(card: { repo: string; name: string }): Promise<string> {
    try {
      return (await fetchPlan(card.repo, card.name)).content;
    } catch (err) {
      return `Couldn't load plan: ${err instanceof Error ? err.message : String(err)}`;
    }
  }

  async function onTogglePlan() {
    if (!planCard) return;
    if (planOpen) {
      setPlanOpen(false);
      return;
    }
    setPlanOpen(true);
    if (planContent !== null) return;
    setPlanContent(await tryLoadPlan(planCard));
  }

  async function onExecutePlan() {
    if (!planCard) return;
    let content = planContent;
    if (content === null) {
      content = await tryLoadPlan(planCard);
      setPlanContent(content);
    }
    if (content.startsWith("Couldn't load plan:")) return;
    onAskAgent(planExecuteSeed(planCard.name, content));
  }

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

  const { startPolling } = useRunPolling({
    setTail,
    setRunning,
    setNotice,
    setPlanCard,
    reload: load,
  });

  useEffect(() => {
    void guarded(load);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load]);

  // Live list: re-fetch while a run is active so completion lands
  // without a manual Refresh. Paused in a hidden tab.
  useEffect(() => {
    if (useMock()) return;
    if (!jobs || (!jobsListActive(jobs) && !running)) return;
    const timer = window.setInterval(() => {
      if (!document.hidden) void guarded(load);
    }, 5000);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobs, running, load]);

  async function onSelectRun(id: string) {
    const r = await fetchJobRun(id);
    setRunId(id);
    setRunning(null);
    setTail(r.output || 'No output yet.');
  }

  async function launch(label: string, start: () => Promise<{ id: string }>) {
    setNotice(null);
    setTail('');
    setPlanCard(null);
    setPlanOpen(false);
    setPlanContent(null);
    let id: string;
    try {
      id = (await start()).id;
    } catch (err) {
      // Single-flight conflict: attach to the run already going
      // instead of erroring (double-tap, slow poll).
      const attached = attachIdFromConflict(
        err instanceof Error ? err.message : String(err),
      );
      if (!attached) throw err;
      setRunId(attached);
      setNotice(`Already running — attached to ${attached}.`);
      if (!useMock()) startPolling(attached, label, repo);
      return;
    }
    setRunId(id);
    setNotice(`Started ${label}: ${id}`);
    if (useMock()) return;
    if (/^[0-9a-f]{12}$/.test(id)) startPolling(id, label, repo);
    else await load();
  }

  // Bring the progress panel into view once it renders for a new run.
  useEffect(() => {
    if (!running) return;
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    progressRef.current?.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'nearest' });
  }, [running]);

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

  function onDangerAction(action: string, label: string) {
    void guarded(() => launch(label, () => startJobRun(action, repo, true)));
  }

  function handleClear(all: boolean) {
    void guarded(() => onClear(all));
  }

  function handleTakeover() {
    void guarded(onTakeover);
  }

  if (!jobs && !error) return <section data-testid="jobs-loading" role="status">Loading…</section>;

  const prOk = /^\d+$/.test(pr.trim());
  const prStateCurrent = prState && String(prState.n) === pr.trim() ? prState : null;
  const list = jobs ?? [];
  const finishedCount = list.filter((j) => j.done).length;
  const fixCiBlock = fixCiBlockReason(prStateCurrent);
  const conflictBlock = conflictBlockReason(prStateCurrent);

  return (
    <section data-testid="jobs-view">
      <JobsHeader list={list} running={running} onRefresh={() => void guarded(load)} />
      {error && (
        <p role="alert" data-testid="jobs-error">
          Couldn&apos;t load jobs: {error}
        </p>
      )}
      {notice && (
        <p className="row-meta" data-testid="jobs-notice">
          {notice}
        </p>
      )}
      <RunProgressPanel tail={tail} runId={runId} running={running} panelRef={progressRef} />
      <PlanCardPanel
        card={planCard}
        open={planOpen}
        content={planContent}
        onToggle={() => void onTogglePlan()}
        onExecute={() => void onExecutePlan()}
      />
      <RepairSection repo={repo} />

      <TdCommands
        pr={pr}
        onPrChange={setPr}
        prStateCurrent={prStateCurrent}
        prOk={prOk}
        fixCiBlock={fixCiBlock}
        conflictBlock={conflictBlock}
        onDispatch={onDispatchTd}
      />

      <DangerZone
        mergeReady={mergeReady}
        finishedCount={finishedCount}
        runCount={list.length}
        onDangerAction={onDangerAction}
        onClear={handleClear}
        onTakeover={handleTakeover}
      />

      <TriageHint />
      <JobsListPanel
        list={list}
        repo={repo}
        cursor={cursor}
        onSelect={(id) => void guarded(() => onSelectRun(id))}
      />
    </section>
  );
}
