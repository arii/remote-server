import type { JobRecord } from '../apiTypes';
import type { IssueRow } from '../reviewTypes';

// The human result of a run: last meaningful output line, skipping the
// `$ ...` command echoes the engine prepends. Empty when nothing surfaced.
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

// Auto-refresh gate: the list re-fetches on an interval only while a
// batch run is still active, so an idle Jobs view makes no traffic.
export function jobsListActive(jobs: JobRecord[]): boolean {
  return jobs.some((j) => !j.done);
}

// PR reference state behind the td dispatch relevance gates. null =
// unknown (fail open: buttons stay enabled).
export interface PrRefState {
  n: number;
  ci: string;
  conflicted: boolean;
}

// Relevance gates: fix-ci only makes sense on a failing PR,
// resolve-conflicts only on a conflicted one.
export function fixCiBlockReason(pr: PrRefState | null): string | null {
  return pr && pr.ci !== 'failing' ? `CI is ${pr.ci} — nothing to fix.` : null;
}

export function conflictBlockReason(pr: PrRefState | null): string | null {
  return pr && !pr.conflicted ? 'No conflicts on this PR.' : null;
}

export function tdBlockReason(slug: string, pr: PrRefState | null): string | null {
  if (slug === 'fix-ci') return fixCiBlockReason(pr);
  if (slug === 'resolve-conflicts') return conflictBlockReason(pr);
  return null;
}

// Danger-zone merge note: null keeps the plain consequence line.
export function mergeNoteFor(action: string, mergeReady: number | null): string | null {
  if (action !== 'merge-approved' || mergeReady === null) return null;
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
  return {
    notice: result ? `Finished ${label}: ${result}` : `Finished ${label}.`,
    planCard,
  };
}
