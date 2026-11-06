import { Fragment, useCallback, useEffect, useRef, useState } from 'react';
import BranchField, { useBranchField } from '../components/BranchField';
import CopyButton from '../components/CopyButton';
import RetryableError from '../components/RetryableError';
import TapToFire from '../components/TapToFire';
import TriageHint from '../components/TriageHint';
import {
  cancelJobRun,
  clearJobs,
  dispatchRepair,
  fetchJobRun,
  fetchJobs,
  fetchPlan,
  fetchRunCompare,
  fetchUxAuditGroups,
  requestTakeover,
  rerunJob,
  startJobRun,
  startTdRun,
  startUxAuditRun,
} from '../apiClient';
import type { RunCompareResult, UxAuditGroup } from '../apiClient';
import type { JobRecord } from '../apiTypes';
import { useTriageKeys } from '../triageKeys';
import { loadJobs, useMock } from './mockViews';
import {
  UX_AUDIT_SITES,
  attachIdFromConflict,
  conflictBlockReason,
  defaultComparePair,
  diffLineTone,
  filterOutputLines,
  isConflictError,
  isNotFoundError,
  isTabVisible,
  jobsListActive,
  onTabVisible,
  parseUnifiedDiffRows,
  planExecuteSeed,
  prFieldSeed,
  resultLine,
  shouldResetPaneOnClear,
  uxAuditPagesFromInput,
  uxAuditSiteRepo,
} from './jobsUtils';
import type { DiffLineTone } from './jobsUtils';
import DangerZone, { dangerEntryFor } from './DangerZone';
import JobSchedulesSection from './JobSchedulesSection';
import type { ScheduleDraft } from './JobSchedulesSection';
import RepairSection from './RepairSection';
import TdCommands from './TdCommands';
import type { TdCommand } from './TdCommands';
import { useMergeReady, usePrRefState } from './useJobsData';
import { useJobsPush } from './useJobsPush';
import { useRunSubscription } from './useRunSubscription';

function labelOf(j: JobRecord): string {
  return j.title || j.name || j.kind || j.id;
}

// Row note excerpt (#1013): error/detail text renders capped with
// the full text on hover — wide rows no longer grow to dialogue
// scale. Pure helper so the truncation boundary is unit-tested.
const JOB_ROW_NOTE_CHARS = 160;

export function excerptJobNote(text: string): string {
  const clean = text.replace(/\s+/g, ' ').trim();
  if (clean.length <= JOB_ROW_NOTE_CHARS) return clean;
  return `${clean.slice(0, JOB_ROW_NOTE_CHARS - 1).trimEnd()}…`;
}

export function JobListItem({
  job,
  repo,
  kbdFocus,
  onSelect,
  onRerun,
  onCancel,
  onSchedule,
}: {
  job: JobRecord;
  repo: string;
  kbdFocus?: boolean;
  onSelect: (id: string) => void;
  onRerun: (job: JobRecord, confirmed: boolean) => void;
  onCancel: (job: JobRecord) => void;
  onSchedule?: (job: JobRecord) => void;
}) {
  const danger = dangerEntryFor(job.kind || job.name || '');
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
      {(job.error || job.detail) && (
        <p className="row-meta job-row-note" title={job.error || job.detail || ''}>
          {excerptJobNote(job.error || job.detail || '')}
        </p>
      )}
      <div className="actions">
        <button
          type="button"
          aria-label={`View output of ${labelOf(job)} ${job.id}`}
          onClick={() => onSelect(job.id)}
        >
          Output
        </button>{' '}
        {job.done &&
          (danger ? (
            <TapToFire
              label="Re-run"
              confirm={danger.confirm}
              testid="job-rerun"
              onFire={() => onRerun(job, true)}
            />
          ) : (
            <button type="button" data-testid="job-rerun" onClick={() => onRerun(job, false)}>
              Re-run
            </button>
          ))}
        {job.done && !danger && onSchedule && (
          <button type="button" data-testid="job-schedule" onClick={() => onSchedule(job)}>
            Schedule…
          </button>
        )}
        {!job.done && (
          <button type="button" data-testid="job-cancel" onClick={() => onCancel(job)}>
            Cancel
          </button>
        )}
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

// One output line with <mark> around case-insensitive query
// matches (#657). Empty query renders the line as-is.
export function HighlightedLine({ line, query }: { line: string; query: string }) {
  const q = query.trim();
  if (!q) return <>{line}</>;
  const lower = line.toLowerCase();
  const needle = q.toLowerCase();
  const parts: React.ReactNode[] = [];
  let i = 0;
  let k = 0;
  for (;;) {
    const at = lower.indexOf(needle, i);
    if (at < 0) break;
    if (at > i) parts.push(<Fragment key={k++}>{line.slice(i, at)}</Fragment>);
    parts.push(<mark key={k++}>{line.slice(at, at + needle.length)}</mark>);
    i = at + needle.length;
  }
  parts.push(<Fragment key={k}>{line.slice(i)}</Fragment>);
  return <>{parts}</>;
}

export function RunProgressPanel({
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
  // Client-only filter over the already-fetched tail (#657). The
  // parent keys this panel by run id, so the query resets per run.
  const [query, setQuery] = useState('');
  if (!tail && !runId && !running) return null;
  const { visible, matched, total } = filterOutputLines(tail, query);
  const filtering = query.trim() !== '';
  return (
    <div
      className="detail jobs-live"
      data-testid="jobs-progress"
      data-live={running ? 'true' : undefined}
      role="status"
      aria-live="polite"
      aria-label={running ? `Live output: running ${running}` : 'Job output'}
      ref={panelRef}
    >
      <span className="row-title">
        {running ? (
          <>
            <span className="dot dot-warn" aria-hidden="true" /> Running {running} — streaming live output…
          </>
        ) : (
          `Output${runId ? ` · ${runId}` : ''}`
        )}
      </span>
      {running && (
        <p className="row-meta" data-testid="jobs-live-status">
          Live — output streams here as the job runs. You can leave this view; the run continues.
        </p>
      )}
      {!running && runId && resultLine(tail) && (
        <p className="row-meta" data-testid="jobs-result">
          {resultLine(tail)}
        </p>
      )}
      <div className="actions">
        <input
          type="search"
          placeholder="Filter output…"
          aria-label="Filter output"
          data-testid="jobs-output-filter"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>
      <p className="row-meta" data-testid="jobs-output-count">
        {filtering ? `${matched} of ${total} lines match` : `${total} lines`}
      </p>
      <div className="actions">
        <CopyButton text={tail} testid="jobs-output-copy" label="Copy output" />
      </div>
      <details open data-testid="jobs-output-wrap">
        <summary className="row-meta">Output</summary>
        <pre className="row-meta">
          {tail === ''
            ? 'Waiting for output…'
            : visible.length === 0
              ? 'No matching lines.'
              : visible.map((l, i) => (
                  <span key={i}>
                    <HighlightedLine line={l} query={query} />
                    {i < visible.length - 1 ? '\n' : ''}
                  </span>
                ))}
        </pre>
      </details>
    </div>
  );
}

const DIFF_TONE_CLASS: Record<DiffLineTone, string> = {
  add: 'tone-ok',
  del: 'tone-bad',
  hunk: 'tone-muted',
};

export function UnifiedDiffView({ diff }: { diff: string }) {
  if (diff === '') return <p className="row-meta">No differences.</p>;
  const lines = diff.split('\n');
  return (
    <>
      <div className="actions">
        <CopyButton text={diff} testid="run-compare-copy" label="Copy diff" />
      </div>
      <pre className="row-meta" data-testid="run-compare-diff">
        {lines.map((line, i) => {
          const tone = diffLineTone(line);
          return (
            <span key={i} className={tone ? DIFF_TONE_CLASS[tone] : undefined}>
              {line}
              {i < lines.length - 1 ? '\n' : ''}
            </span>
          );
        })}
      </pre>
    </>
  );
}

export function SideBySideDiffView({
  diff,
  aId,
  bId,
}: {
  diff: string;
  aId: string;
  bId: string;
}) {
  const rows = parseUnifiedDiffRows(diff);
  if (rows.length === 0) return <p className="row-meta">No differences.</p>;
  return (
    <table className="row-meta" data-testid="run-compare-side">
      <thead>
        <tr>
          <th>{aId}</th>
          <th>{bId}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r, i) =>
          r.header ? (
            <tr key={i}>
              <td colSpan={2} className={DIFF_TONE_CLASS.hunk}>
                {r.left}
              </td>
            </tr>
          ) : (
            <tr key={i}>
              <td className={r.leftTone ? DIFF_TONE_CLASS[r.leftTone] : undefined}>
                {r.left ?? ''}
              </td>
              <td className={r.rightTone ? DIFF_TONE_CLASS[r.rightTone] : undefined}>
                {r.right ?? ''}
              </td>
            </tr>
          ),
        )}
      </tbody>
    </table>
  );
}

// Ephemeral pairwise output comparison (#657). Pickers default to
// the two latest finished runs (older first); the diff refetches
// whenever either pick changes. Hidden with fewer than two
// finished runs.
export function RunComparePanel({ jobs }: { jobs: JobRecord[] }) {
  const finished = jobs.filter((j) => j.done);
  const ids = finished.map((j) => j.id);
  const defaults = defaultComparePair(ids);
  const [aPick, setAPick] = useState<string | null>(null);
  const [bPick, setBPick] = useState<string | null>(null);
  const [mode, setMode] = useState<'unified' | 'side'>('unified');
  const [result, setResult] = useState<RunCompareResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  // Picks fall back to the defaults when the picked run is gone
  // (cleared) or was never picked.
  const a = aPick && ids.includes(aPick) ? aPick : (defaults?.[0] ?? null);
  const b = bPick && ids.includes(bPick) ? bPick : (defaults?.[1] ?? null);
  useEffect(() => {
    if (!a || !b) {
      setResult(null);
      return;
    }
    let live = true;
    setLoading(true);
    setError(null);
    fetchRunCompare(a, b).then(
      (r) => {
        if (!live) return;
        setResult(r);
        setLoading(false);
      },
      (err) => {
        if (!live) return;
        setError(err instanceof Error ? err.message : String(err));
        setLoading(false);
      },
    );
    return () => {
      live = false;
    };
  }, [a, b, nonce]);
  if (!defaults) return null;
  const optionLabel = (id: string) => {
    const j = finished.find((f) => f.id === id);
    return j ? `${id} · ${labelOf(j)}` : id;
  };
  return (
    <div className="detail" data-testid="run-compare">
      <span className="row-title">Compare runs</span>
      <div className="actions">
        <label className="row-meta">
          A{' '}
          <select
            data-testid="run-compare-a"
            value={a ?? ''}
            onChange={(e) => setAPick(e.target.value || null)}
          >
            {ids.map((id) => (
              <option key={id} value={id}>
                {optionLabel(id)}
              </option>
            ))}
          </select>
        </label>{' '}
        <label className="row-meta">
          B{' '}
          <select
            data-testid="run-compare-b"
            value={b ?? ''}
            onChange={(e) => setBPick(e.target.value || null)}
          >
            {ids.map((id) => (
              <option key={id} value={id}>
                {optionLabel(id)}
              </option>
            ))}
          </select>
        </label>{' '}
        <button
          type="button"
          data-testid="run-compare-mode"
          onClick={() => setMode((m) => (m === 'unified' ? 'side' : 'unified'))}
        >
          {mode === 'unified' ? 'Side-by-side' : 'Unified'}
        </button>
      </div>
      {loading && <p className="row-meta" role="status">Loading diff…</p>}
      {error && (
        <RetryableError
          testid="run-compare-error"
          message={`Couldn't load the diff: ${error}`}
          retryTestid="run-compare-retry"
          onRetry={() => {
            setError(null);
            setNonce((n) => n + 1);
          }}
        />
      )}
      {!loading && !error && result && (
        <>
          {result.truncated && (
            <p className="row-meta">Outputs truncated to 500K chars per side.</p>
          )}
          {mode === 'unified' ? (
            <UnifiedDiffView diff={result.unified_diff} />
          ) : (
            <SideBySideDiffView diff={result.unified_diff} aId={result.a_id} bId={result.b_id} />
          )}
        </>
      )}
    </div>
  );
}

export function PlanCardPanel({
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
        </button>{' '}
        {content && <CopyButton text={content} testid="jobs-plan-copy" label="Copy plan" />}
      </div>
      {open && <pre className="row-meta">{content ?? 'Loading plan…'}</pre>}
    </div>
  );
}

// Finding groups from the latest audit report (#850): one agent
// session per group via the normal repair dispatch (issue + site
// repo + group task spec as focus). Fixes then flow through the
// existing Review/merge pane.
export function UxAuditGroups({ site }: { site: string }) {
  const [groups, setGroups] = useState<UxAuditGroup[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [issue, setIssue] = useState('');
  const [assigning, setAssigning] = useState<number | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (useMock()) {
      setGroups([]);
      return;
    }
    setError(null);
    try {
      const r = await fetchUxAuditGroups(site);
      setGroups(r.groups);
    } catch (err) {
      setGroups(null);
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [site]);

  useEffect(() => {
    void load();
  }, [load]);

  async function onAssign(group: UxAuditGroup) {
    const target = issue.trim().replace(/^#+/, '');
    if (!/^\d+$/.test(target)) {
      setMessage('Enter the findings issue number first.');
      return;
    }
    const repo = uxAuditSiteRepo(site);
    if (!repo) {
      setMessage(`Unknown site repo for ${site}.`);
      return;
    }
    setAssigning(group.index);
    setMessage(null);
    try {
      const r = await dispatchRepair(target, undefined, repo, group.body);
      setMessage(`Assigned group ${group.index} (${group.category}): ${r.repo}#${r.issue} ${r.status}.`);
    } catch (err) {
      setMessage(`Assign failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setAssigning(null);
    }
  }

  return (
    <div>
      <p className="row-meta">
        Latest findings for {site}
        {groups !== null ? ` (${groups.length} group${groups.length === 1 ? '' : 's'})` : ''} ·{' '}
        <button type="button" data-testid="ux-audit-refresh" onClick={() => void load()}>
          Refresh
        </button>
      </p>
      {error && (
        <p role="alert" data-testid="ux-audit-groups-error">
          Couldn&apos;t load findings: {error}
        </p>
      )}
      {groups === null && !error && (
        <p className="row-meta" data-testid="ux-audit-groups-loading" role="status">
          Loading findings…
        </p>
      )}
      {message && (
        <p className="row-meta" data-testid="ux-audit-assign-message">
          {message}
        </p>
      )}
      {groups !== null && groups.length > 0 && (
        <>
          <label className="row-meta" htmlFor="ux-audit-issue">
            Findings issue{' '}
            <input
              id="ux-audit-issue"
              data-testid="ux-audit-issue"
              value={issue}
              onChange={(e) => setIssue(e.target.value)}
              placeholder="# from the AUDIT_DONE line…"
              inputMode="numeric"
              size={8}
            />
          </label>
          <ul>
            {groups.map((g) => (
              <li key={g.index} className="detail" data-testid={`ux-audit-group-${g.index}`}>
                <span className="row-title">
                  Group {g.index}: {g.category}
                </span>
                <p className="row-meta">
                  {g.count} finding{g.count === 1 ? '' : 's'} · worst: {g.worst}
                </p>
                <div className="actions">
                  <button
                    type="button"
                    data-testid={`ux-audit-assign-${g.index}`}
                    disabled={assigning === g.index}
                    onClick={() => void onAssign(g)}
                  >
                    {assigning === g.index ? 'Assigning…' : 'Assign group'}
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

// Dispatched UX audit launcher (#850): site picker + page cap +
// Launch through the same launch path as td commands, so live
// output streams via the run subscription and the finished row
// inherits re-run, cancel, and scheduling.
export function UxAuditSection({
  onDispatch,
}: {
  onDispatch: (site: string, maxPages: number) => void;
}) {
  const [site, setSite] = useState<string>(UX_AUDIT_SITES[0]);
  const [pages, setPages] = useState('25');
  return (
    <section aria-label="UX audit">
      <h3>UX audit</h3>
      <p className="row-meta">
        Multi-page impeccable audit; files one findings issue to the site&apos;s repo.
      </p>
      <div className="actions">
        <label className="row-meta" htmlFor="ux-audit-site">
          Site{' '}
          <select
            id="ux-audit-site"
            data-testid="ux-audit-site"
            value={site}
            onChange={(e) => setSite(e.target.value)}
          >
            {UX_AUDIT_SITES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>{' '}
        <label className="row-meta" htmlFor="ux-audit-pages">
          Pages{' '}
          <input
            id="ux-audit-pages"
            data-testid="ux-audit-pages"
            value={pages}
            onChange={(e) => setPages(e.target.value)}
            inputMode="numeric"
            size={4}
          />
        </label>{' '}
        <button
          type="button"
          data-testid="dispatch-ux-audit"
          onClick={() => onDispatch(site, uxAuditPagesFromInput(pages))}
        >
          Run UX audit
        </button>
      </div>
      <UxAuditGroups site={site} />
    </section>
  );
}

// Triage fanout launcher (#670): a one-repo sweep for the current
// repo. Dispatches through the same launch path as td commands, so
// live output streams via the run subscription and the finished row
// inherits re-run, cancel, and scheduling.
export function TriageFanoutSection({
  repo,
  onDispatch,
}: {
  repo: string;
  onDispatch: () => void;
}) {
  return (
    <section aria-label="Triage fanout">
      <h3>Triage fanout</h3>
      <p className="row-meta">
        Per-repo triage sweep for {repo}; local runs can take 10+ minutes.
      </p>
      <div className="actions">
        <button type="button" data-testid="dispatch-triage-fanout" onClick={onDispatch}>
          Run triage fanout
        </button>
      </div>
    </section>
  );
}

export function JobsListPanel({
  list,
  repo,
  cursor,
  onSelect,
  onRerun,
  onCancel,
  onSchedule,
  onOpenChat,
}: {
  list: JobRecord[];
  repo: string;
  cursor: number | null;
  onSelect: (id: string) => void;
  onRerun: (job: JobRecord, confirmed: boolean) => void;
  onCancel: (job: JobRecord) => void;
  onSchedule: (job: JobRecord) => void;
  onOpenChat?: () => void;
}) {
  if (list.length === 0) {
    return (
      <div data-testid="jobs-empty">
        <h3>No batch runs</h3>
        <p className="row-meta">
          Dispatch one above. Agent chat runs live under Chat → Past runs.
        </p>
        {onOpenChat && (
          <div className="actions">
            <button type="button" data-testid="jobs-empty-chat" onClick={onOpenChat}>
              Open Chat past runs
            </button>
          </div>
        )}
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
          onRerun={onRerun}
          onCancel={onCancel}
          onSchedule={onSchedule}
        />
      ))}
    </ul>
  );
}

export default function JobsView({
  repo,
  defaultBranch,
  triageSuspended = false,
  onAskAgent,
  initialPr = null,
  onOpenChat,
  pendingTdCommand = null,
  onPendingTdHandled,
}: {
  repo: string;
  // Checkout branch of the viewed repo: seeds the launch branch
  // field and the repair dispatch (#708, #435).
  defaultBranch?: string;
  // Drawer-open flag: while true (agent drawer / PTY terminal open),
  // triage keys are disabled.
  triageSuspended?: boolean;
  onAskAgent: (seed: string) => void;
  // Review row selection from Now: seeds the PR field on mount.
  // Jobs remounts per section visit, so each visit picks up the latest.
  initialPr?: number | null;
  // Empty-state hop to Chat past runs (#907).
  onOpenChat?: () => void;
  // Palette-dispatched td command (#1057): launched once through the
  // same progress/notice path as the Jobs buttons.
  pendingTdCommand?: { command: string; nonce: number } | null;
  onPendingTdHandled?: (nonce: number) => void;
}) {
  const [jobs, setJobs] = useState<JobRecord[] | null>(null);
  const [branch, setBranch] = useBranchField(repo, defaultBranch);
  const [error, setError] = useState<string | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [running, setRunning] = useState<string | null>(null);
  const [tail, setTail] = useState('');
  const [notice, setNotice] = useState<string | null>(null);
  const [pr, setPr] = useState(prFieldSeed(initialPr));
  // Relevance state. null = unknown (fail open: buttons stay enabled).
  const mergeReady = useMergeReady(repo);
  const prState = usePrRefState(pr, repo);
  const [planCard, setPlanCard] = useState<{ repo: string; name: string } | null>(null);
  // Prefill for the schedules section, carried from a finished
  // run's Schedule control (same replay params as re-run).
  const [schedDraft, setSchedDraft] = useState<ScheduleDraft | null>(null);
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

  // Contextual error surface (#965): backend failures render
  // inline with the action that caused them, never silent and
  // never mislabeled as a load error. Callers pass the verb phrase
  // for their action; the default keeps today's load copy.
  async function guarded(fn: () => Promise<void>, action = 'load jobs') {
    setError(null);
    try {
      await fn();
    } catch (err) {
      setError(`Couldn't ${action}: ${err instanceof Error ? err.message : String(err)}`);
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

  const { trackRun } = useRunSubscription({
    setTail,
    setRunning,
    setNotice,
    setPlanCard,
    reload: load,
  });

  // Push-first list: the jobs stream replaces polling while the
  // channel is live (Now embeds this view, so it inherits push).
  const pushLive = useJobsPush(setJobs);

  useEffect(() => {
    void guarded(load);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load]);

  // Fallback list poll: re-fetch while a run is active and the push
  // channel is down, so completion lands without a manual Refresh.
  // Paused in a hidden tab; a returning tab refreshes immediately.
  useEffect(() => {
    if (useMock()) return;
    if (pushLive) return;
    if (!jobs || (!jobsListActive(jobs) && !running)) return;
    const timer = window.setInterval(() => {
      if (isTabVisible()) void guarded(load);
    }, 5000);
    const stopResume = onTabVisible(() => void guarded(load));
    return () => {
      window.clearInterval(timer);
      stopResume();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobs, running, load, pushLive]);

  async function onSelectRun(id: string) {
    const r = await fetchJobRun(id);
    setRunId(id);
    setRunning(null);
    setTail(r.output || 'No output yet.');
  }

  async function onRerun(job: JobRecord, confirmed: boolean) {
    // Liveness gate: the registry is in-memory, so a run cleared
    // or restarted away since the list snapshot 404s here instead
    // of launching from a corpse. Params replay from the row, which
    // carries them without a second fetch.
    try {
      await fetchJobRun(job.id);
    } catch (err) {
      if (isNotFoundError(err instanceof Error ? err.message : String(err))) {
        setNotice(`Run ${job.id} is no longer available.`);
        await load();
        return;
      }
      throw err;
    }
    const action = job.kind || job.name || '';
    const argv = job.td_argv ?? [];
    const target = job.repo || repo;
    // The branch field seeds from the viewed repo: only send it
    // when the re-run targets that same repo, otherwise omit and
    // keep today's server behavior.
    const targetBranch = target === repo ? branch : undefined;
    await launch(`re-run ${job.id}`, () =>
      rerunJob(action, target, argv, confirmed, targetBranch),
    );
  }

  function onScheduleJob(job: JobRecord) {
    setSchedDraft({
      action: job.kind || job.name || '',
      td_argv: job.td_argv ?? [],
      repo: job.repo || repo,
    });
    setNotice(`Scheduling ${job.id} — pick a repeat below.`);
  }

  async function onCancelRun(job: JobRecord) {
    // No confirm: cancel stops spend. Stale rows resolve to
    // notices instead of errors: 404 when the record is gone,
    // 409 when the run finished between render and tap.
    try {
      await cancelJobRun(job.id);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      if (isNotFoundError(message)) {
        setNotice(`Run ${job.id} is no longer available.`);
        await load();
        return;
      }
      if (isConflictError(message)) {
        setNotice(`Run ${job.id} already finished.`);
        await load();
        return;
      }
      throw err;
    }
    setNotice(`Cancelled ${job.id}.`);
    await load();
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
      if (!useMock()) trackRun(attached, label, repo);
      return;
    }
    setRunId(id);
    setNotice(`Started ${label}: ${id}`);
    if (useMock()) return;
    if (/^[0-9a-f]{12}$/.test(id)) trackRun(id, label, repo);
    else await load();
  }

  // Palette-dispatched td command (#1057): launch once through the
  // same progress/notice path as the Jobs buttons. Nonce-guarded so a
  // revisit never refires a stale request.
  const handledTdNonce = useRef(0);
  useEffect(() => {
    if (pendingTdCommand && pendingTdCommand.nonce !== handledTdNonce.current) {
      handledTdNonce.current = pendingTdCommand.nonce;
      const { command, nonce } = pendingTdCommand;
      onPendingTdHandled?.(nonce);
      void guarded(() => launch(command, () => startTdRun(command, repo, branch)));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingTdCommand]);

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
    void guarded(() => launch(cmd.label, () => startTdRun(cmd.build(n), repo, branch)));
  }

  async function onClear(all: boolean) {
    const r = await clearJobs(all);
    setNotice(`Cleared ${r.cleared} job(s).`);
    if (shouldResetPaneOnClear(r.cleared, running)) {
      setRunId(null);
      setTail('');
      setPlanCard(null);
      setPlanOpen(false);
      setPlanContent(null);
    }
    await load();
  }

  async function onTakeover() {
    const r = await requestTakeover();
    setNotice(r.matched === false
      ? 'No active client to take over from.'
      : 'Takeover requested.');
  }

  function onDangerAction(action: string, label: string) {
    void guarded(() => launch(label, () => startJobRun(action, repo, true, branch)));
  }

  function handleClear(all: boolean) {
    void guarded(() => onClear(all), 'clear runs');
  }

  function handleTakeover() {
    void guarded(onTakeover, 'request terminal takeover');
  }

  if (!jobs && !error) return <section data-testid="jobs-loading" role="status">Loading jobs…</section>;

  const prOk = /^\d+$/.test(pr.trim());
  const prStateCurrent = prState && String(prState.n) === pr.trim() ? prState : null;
  const list = jobs ?? [];
  const finishedCount = list.filter((j) => j.done).length;
  const conflictBlock = conflictBlockReason(prStateCurrent);

  return (
    <section data-testid="jobs-view">
      <JobsHeader list={list} running={running} onRefresh={() => void guarded(load)} />
      {error && (
        <p role="alert" data-testid="jobs-error">
          {error}
        </p>
      )}
      {notice && (
        <p className="jobs-notice" data-testid="jobs-notice" role="status" aria-live="polite">
          {notice}
        </p>
      )}
      {running && (
        <p className="row-meta" data-testid="jobs-running-banner" role="status" aria-live="polite">
          Running {running} — live output below.
        </p>
      )}
      <div className="actions">
        <BranchField
          id="jobs-branch"
          testid="jobs-branch"
          value={branch}
          onChange={setBranch}
        />
      </div>
      <RunProgressPanel
        key={runId ?? 'none'}
        tail={tail}
        runId={runId}
        running={running}
        panelRef={progressRef}
      />
      <RunComparePanel jobs={list} />
      <PlanCardPanel
        card={planCard}
        open={planOpen}
        content={planContent}
        onToggle={() => void onTogglePlan()}
        onExecute={() => void onExecutePlan()}
      />

      <section className="jobs-workflows" data-testid="jobs-workflows" aria-label="Agent Workflows & Diagnostics">
        <h3>Agent Workflows &amp; Diagnostics</h3>
        <p className="row-meta">
          Safe, non-disruptive agent runs and diagnostics. Triggering shows live streaming output above — no service is restarted.
        </p>

        <RepairSection repo={repo} defaultBranch={defaultBranch} />

        <TriageFanoutSection
          repo={repo}
          onDispatch={() =>
            void guarded(() => launch('triage fanout', () => startJobRun('triage-fanout', repo, false, branch)))
          }
        />

        <UxAuditSection
          onDispatch={(site, maxPages) =>
            void guarded(() => launch(`ux audit ${site}`, () => startUxAuditRun(site, maxPages)))
          }
        />

        <section aria-label="Briefing plan">
          <h3>Briefing plan</h3>
          <p className="row-meta">Writes a dated plan file into .agents/plans for the briefing.</p>
          <div className="actions">
            <button
              type="button"
              data-testid="dispatch-create-plan"
              onClick={() => onDangerAction('create-plan', 'Write briefing plan')}
            >
              Write briefing plan
            </button>
          </div>
        </section>

        <TdCommands
          pr={pr}
          onPrChange={setPr}
          prStateCurrent={prStateCurrent}
          prOk={prOk}
          conflictBlock={conflictBlock}
          onDispatch={onDispatchTd}
        />

        <JobSchedulesSection
          repo={repo}
          draft={schedDraft}
          onScheduled={(s) => setNotice(`Scheduled ${s.action}: ${s.id}`)}
          onClearDraft={() => setSchedDraft(null)}
        />
      </section>

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
        onRerun={(job, confirmed) => void guarded(() => onRerun(job, confirmed))}
        onCancel={(job) => void guarded(() => onCancelRun(job))}
        onSchedule={onScheduleJob}
        onOpenChat={onOpenChat}
      />
    </section>
  );
}
