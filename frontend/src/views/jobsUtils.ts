import type { JobRecord } from '../apiTypes';
import type { IssueRow } from '../reviewTypes';

// The human result of a run: last meaningful output line, skipping the
// `$ ...` command echoes the engine prepends. Empty when nothing surfaced.
// Display label for a palette-dispatched td command: the raw argv
// ("td doctor") reads as a command, not a finished check, in the
// progress/notice path (e2e jobs-run asserts "Finished Doctor: …").
export function tdCommandLabel(command: string): string {
  if (command === 'td doctor') return 'Doctor';
  if (command === 'td preflight') return 'Preflight';
  return command;
}

export function resultLine(output: string): string {
  const lines = output
    .split('\n')
    .map((l) => l.trim())
    .filter((l) => l.length > 0 && !l.startsWith('$'));
  return lines.length > 0 ? lines[lines.length - 1] : '';
}

// Active run id from a 409 conflict message
// ("... 409 {...already in progress: <id>...}"), or null.
export function attachIdFromConflict(message: string): string | null {
  if (!/: 409\b/.test(message)) return null;
  return /already in progress: ([0-9a-f]{12})/.exec(message)?.[1] ?? null;
}

// Gone-record detection for re-run liveness: the registry is
// in-memory, so a cleared/restarted-away run 404s on progress.
export function isNotFoundError(message: string): boolean {
  return /: 404\b/.test(message);
}

// Finished-record detection for cancel: the run completed between
// the list snapshot and the tap, so cancel 409s.
export function isConflictError(message: string): boolean {
  return /: 409\b/.test(message);
}

// Plan filename from a create-plan result line ("wrote <path>.md").
export function planNameFromResult(result: string): string | null {
  const m = result.trim().match(/^wrote\s+(.+\.md)$/);
  if (!m) return null;
  const parts = m[1].split('/');
  return parts[parts.length - 1] || null;
}

// Chat seed that hands a written plan to the agent for execution.
// Chat auto-sends seeds, so one tap dispatches with live progress.
export function planExecuteSeed(name: string, content: string): string {
  return `Execute this plan step by step (${name}). Report progress as you go.\n\n${content}`;
}

// Agent-commands PR field seed from the Review row selection held by
// Now. Empty with no selection (field works standalone as before).
export function prFieldSeed(selectedPr: number | null): string {
  return selectedPr === null ? '' : String(selectedPr);
}

// Clearing drops the selected run's output pane — unless a run is live
// (the pane shows its streaming output) or nothing cleared.
export function shouldResetPaneOnClear(cleared: number, running: string | null): boolean {
  return cleared > 0 && running === null;
}

// Repair worker candidates: skip issues that are not actionable
// right now (blocked on a decision/lane, or scheduled future work).
// A manual number still overrides the filter.
export function repairCandidates(issues: IssueRow[]): IssueRow[] {
  return issues.filter(
    (i) =>
      !(i.labels ?? []).some((l) => {
        const name = typeof l === 'string' ? l : l.name;
        return name === 'blocked' || name === 'scheduled';
      }),
  );
}

// Substring filter for the repair picker: number or title,
// case-insensitive, leading '#' ignored. Empty query keeps all.
export function filterRepairCandidates(cands: IssueRow[], query: string): IssueRow[] {
  const q = query.trim().replace(/^#+/, '').toLowerCase();
  if (!q) return cands;
  return cands.filter(
    (i) => String(i.number).includes(q) || (i.title ?? '').toLowerCase().includes(q),
  );
}

// Auto-refresh gate: the list re-fetches on an interval only while a
// batch run is still active, so an idle Jobs view makes no traffic.
export function jobsListActive(jobs: JobRecord[]): boolean {
  return jobs.some((j) => !j.done);
}

// Tab-visibility gate for Jobs polling (#401): every Jobs poll
// skips its fetch while the tab is hidden (locked screen,
// background tab) so a hidden console makes no traffic. Only the
// 'hidden' state pauses; prerender and unloaded tabs still poll.
// Fails open without a DOM (SSR, node vitest).
export function isTabVisible(): boolean {
  return typeof document === 'undefined' || document.visibilityState !== 'hidden';
}

// Immediate resume: invokes fn each time the tab turns visible
// again. Returns an unsubscribe for effect/imperative cleanup.
export function onTabVisible(fn: () => void): () => void {
  const onChange = () => {
    if (isTabVisible()) fn();
  };
  document.addEventListener('visibilitychange', onChange);
  return () => document.removeEventListener('visibilitychange', onChange);
}

// PR reference state behind the td dispatch relevance gates. null =
// unknown (fail open: buttons stay enabled).
export interface PrRefState {
  n: number;
  ci: string;
  conflicted: boolean;
}

// Relevance gate: resolve-conflicts only makes sense on a
// conflicted PR. (fix-ci had a failing-CI gate until #428
// retired the command.)
export function conflictBlockReason(pr: PrRefState | null): string | null {
  return pr && !pr.conflicted ? 'No conflicts on this PR.' : null;
}

export function tdBlockReason(slug: string, pr: PrRefState | null): string | null {
  if (slug === 'resolve-conflicts') return conflictBlockReason(pr);
  return null;
}

// Danger-zone merge note: null keeps the plain consequence line
// (non-merge rows only). Unknown merge set reports the load
// failure so the row never silently enables a blind merge-all.
export function mergeNoteFor(action: string, mergeReady: number | null): string | null {
  if (action !== 'merge-approved') return null;
  if (mergeReady === null) return "Couldn't load PRs — merge set unknown.";
  return mergeReady === 0
    ? 'No approved, green PRs right now.'
    : `${mergeReady} ready to merge.`;
}

// Repair worker failure line for the status alert. null when the run
// did not fail.
export function repairFailureMessage(
  status: string,
  issue: number | null,
  exitCode: number | null,
): string | null {
  if (status !== 'failed') return null;
  return `Repair run for #${issue ?? '?'} failed${exitCode !== null ? ` (exit ${exitCode})` : ''}.`;
}

// Verify-command preview line for the repair picker: the confirmer
// sees exactly what the worker would run. Null when there is no
// target yet or the preview failed to load (dispatch then reports
// the server verdict).
export function repairVerifyLine(preview: {
  command: string;
  allowlisted: boolean;
  error: string;
} | null): string | null {
  if (!preview) return null;
  if (preview.allowlisted) return `Verify: ${preview.command}`;
  if (preview.command) return `Verify blocked: ${preview.error}`;
  return `Verify unavailable: ${preview.error}`;
}

// Dispatch is refused up front only for an explicitly rejected
// command; unparseable issues stay clickable so the server explains.
export function repairVerifyBlocksDispatch(preview: {
  command: string;
  allowlisted: boolean;
} | null): boolean {
  return preview !== null && !preview.allowlisted && preview.command !== '';
}

// Auditable sites for the dispatched UX audit agent (#850).
// The backend runner owns the crawl/file registry; this map only
// routes the console's assign dispatch to the site's repo.
export const UX_AUDIT_SITES = ['arii.github.io', 'boomtick.blog'] as const;

const UX_AUDIT_SITE_REPOS: Record<string, string> = {
  'arii.github.io': 'arii.github.io',
  'boomtick.blog': 'boomtick',
};

export function uxAuditSiteRepo(site: string): string | null {
  return UX_AUDIT_SITE_REPOS[site] ?? null;
}

// Filed-issue record from a finished audit run tail
// ("AUDIT_DONE <repo>#<n> <url>"), or null when the run did not
// file (failure, or a report-only invocation).
export interface AuditDone {
  repo: string;
  number: number;
  url: string;
}

export function auditDoneFromOutput(output: string): AuditDone | null {
  const m = /^AUDIT_DONE (\S+)#(\d+) (\S+)\s*$/m.exec(output);
  if (!m) return null;
  return { repo: m[1], number: Number(m[2]), url: m[3] };
}

// Page cap for the audit picker: integer 1..200, default 25 on
// anything unparseable (empty field, pasted text).
export function uxAuditPagesFromInput(raw: string): number {
  const n = Number.parseInt(raw.trim(), 10);
  if (!Number.isFinite(n)) return 25;
  return Math.min(200, Math.max(1, n));
}

// The settled outcome of a polled run: notice line plus the plan card
// a create-plan run produced (if its result line names one).
export interface CompletionInput {
  runId: string;
  action: string;
  output: string;
  exitCode: number | null;
  label: string;
  repo: string;
}

export interface CompletionOutcome {
  notice: string;
  planCard: { repo: string; name: string } | null;
}

export function completionOutcome(input: CompletionInput): CompletionOutcome {
  const label = input.label || 'Run';
  const result = resultLine(input.output);
  if (input.exitCode !== 0 && input.exitCode !== null) {
    return {
      notice: `Run ${input.runId} failed (exit ${input.exitCode})${result ? `: ${result}` : '.'}`,
      planCard: null,
    };
  }
  let planCard: { repo: string; name: string } | null = null;
  if (input.action === 'create-plan') {
    const name = planNameFromResult(result);
    if (name) planCard = { repo: input.repo, name };
  }
  if (input.action === 'ux-audit') {
    const done = auditDoneFromOutput(input.output);
    if (done) {
      return {
        notice: `Finished ${label}: filed ${done.repo}#${done.number} — ${done.url}`,
        planCard,
      };
    }
  }
  return {
    notice: result ? `Finished ${label}: ${result}` : `Finished ${label}.`,
    planCard,
  };
}

// Within-run output filter (#657): case-insensitive substring over
// lines. Empty query keeps all lines.
export interface OutputFilterResult {
  visible: string[];
  matched: number;
  total: number;
}

export function filterOutputLines(output: string, query: string): OutputFilterResult {
  const lines = output === '' ? [] : output.split('\n');
  const q = query.trim().toLowerCase();
  if (!q) return { visible: lines, matched: lines.length, total: lines.length };
  const visible = lines.filter((l) => l.toLowerCase().includes(q));
  return { visible, matched: visible.length, total: lines.length };
}

// Compare picker defaults (#657): the two latest finished ids from a
// newest-first id list, or null when fewer than two finished runs.
// a is the older run so the diff reads old → new.
export function defaultComparePair(finishedIds: string[]): [string, string] | null {
  if (finishedIds.length < 2) return null;
  return [finishedIds[1], finishedIds[0]];
}

// Unified-diff line tone for added/removed coloring (#657). File
// headers (---/+++) and hunk headers (@@) read as hunk, not add/del.
export type DiffLineTone = 'add' | 'del' | 'hunk';

export function diffLineTone(line: string): DiffLineTone | null {
  // Spaced header forms only: a removed content line starting with
  // '--' (e.g. '---- foo') must still read as del, not hunk.
  if (line.startsWith('+++ ') || line.startsWith('--- ') || line.startsWith('@@')) return 'hunk';
  if (line.startsWith('+')) return 'add';
  if (line.startsWith('-')) return 'del';
  return null;
}

// One side-by-side row parsed from a unified diff (#657). Changed
// lines pair old+new on one row; pure additions/removals leave the
// other side null (gutter gap). Hunk headers span both columns.
export interface SideBySideRow {
  left: string | null;
  right: string | null;
  leftTone: DiffLineTone | null;
  rightTone: DiffLineTone | null;
  header: boolean;
}

export function parseUnifiedDiffRows(diff: string): SideBySideRow[] {
  const rows: SideBySideRow[] = [];
  let pendingDel: string[] = [];
  const flush = () => {
    for (const old of pendingDel) {
      rows.push({ left: old, right: null, leftTone: 'del', rightTone: null, header: false });
    }
    pendingDel = [];
  };
  for (const line of diff.split('\n')) {
    if (line === '' || line.startsWith('--- ') || line.startsWith('+++ ')) continue;
    if (line.startsWith('@@')) {
      flush();
      rows.push({ left: line, right: line, leftTone: 'hunk', rightTone: 'hunk', header: true });
      continue;
    }
    if (line.startsWith('-')) {
      pendingDel.push(line.slice(1));
      continue;
    }
    if (line.startsWith('+')) {
      const old = pendingDel.shift();
      if (old === undefined) {
        rows.push({ left: null, right: line.slice(1), leftTone: null, rightTone: 'add', header: false });
      } else {
        rows.push({ left: old, right: line.slice(1), leftTone: 'del', rightTone: 'add', header: false });
      }
      continue;
    }
    flush();
    rows.push({
      left: line.slice(1),
      right: line.slice(1),
      leftTone: null,
      rightTone: null,
      header: false,
    });
  }
  flush();
  return rows;
}
