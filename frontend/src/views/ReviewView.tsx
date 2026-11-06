import { useCallback, useEffect, useRef, useState } from 'react';
import BranchField, { useBranchField } from '../components/BranchField';
import CopyButton from '../components/CopyButton';
import RetryableError from '../components/RetryableError';
import TapToFire from '../components/TapToFire';
import TriageHint from '../components/TriageHint';
import {
  approvePr,
  createIssue,
  deferRow,
  deferRowsBulk,
  draftIssue,
  editIssueLabel,
  fetchDeferred,
  fetchIssues,
  fetchPrDetails,
  fetchPrDiff,
  fetchPrs,
  fetchRepoLabels,
  mergePr,
  requestPrChanges,
  restoreRow,
} from '../apiClient';
import type { PrDiffResponse } from '../apiClient';
import type { DeferredRow, IssueRow, PrDetailsResponse, PrRow } from '../reviewTypes';
import { approveBlockReason, approveMergeEligible, detailsMergeBlockReason, isDependabotPr, isOwnPr, mergeBlockReason } from '../reviewTypes';
import { useTriageKeys } from '../triageKeys';
import { loadIssues, loadPrDetails, loadPrs, useMock } from './mockViews';

export function applyOptimisticMerge(merging: ReadonlySet<number>, n: number): ReadonlySet<number> {
  if (merging.has(n)) return merging;
  const next = new Set(merging);
  next.add(n);
  return next;
}

export function revertOptimisticMerge(merging: ReadonlySet<number>, n: number): ReadonlySet<number> {
  if (!merging.has(n)) return merging;
  const next = new Set(merging);
  next.delete(n);
  return next;
}

// Issue rows have no in-app detail view: opening one means following
// its GitHub URL in a new tab (triage state stays put). No-op without
// a URL or outside the DOM.
export function openIssueUrl(url: string | undefined): void {
  if (!url || typeof window === 'undefined') return;
  window.open(url, '_blank', 'noopener');
}

// Issue row title: a link when the row carries a URL, plain text
// otherwise (older payloads, fixtures without urls).
// Compact merge-state badge for a PR row: the block reason when
// the row's Merge is disabled, merge-clean otherwise. Same helper
// drives both, so the badge and the disabled state can never
// disagree — a responsible merge no longer needs the details pane
// to learn the block state (#596).
export function mergeBadge(p: PrRow): string {
  const blocked = mergeBlockReason(p);
  return blocked ? `Blocked: ${blocked}` : 'merge-clean';
}

export function IssueRowTitle({ issue }: { issue: IssueRow }) {
  const text = `#${issue.number} ${issue.title}`;
  if (!issue.url) return <>{text}</>;
  return (
    <a href={issue.url} target="_blank" rel="noreferrer">
      {text}
    </a>
  );
}

// Post-merge list refresh (#472): the merged PR is closed, so it
// leaves the open-PR list instead of lingering with a stuck
// "Merging…" flag. Null-safe: a list that never loaded stays null.
export function dropMergedPr(prs: readonly PrRow[] | null, n: number): PrRow[] | null {
  if (!prs) return prs;
  return prs.filter((r) => r.number !== n);
}

// Next PR row to auto-advance to after a successful merge, in current
// list order. Skips the merged row; wraps to the first row when the
// merged one was last. Returns null when there is no other PR (single
// row, empty list, or merged number not in the list) so callers keep
// today's notice-only behavior.
export function nextPrAfterMerge(
  prNumbers: readonly number[],
  mergedNumber: number,
): number | null {
  if (prNumbers.length <= 1) return null;
  const idx = prNumbers.indexOf(mergedNumber);
  if (idx === -1) return null;
  return prNumbers[(idx + 1) % prNumbers.length] ?? null;
}

// True when a (kind, number) row carries a deferred flag. Deferred
// rows hide from the main Review lists and surface in the Deferred
// disclosure instead; defer is reversible via Restore.
export function isRowDeferred(
  deferred: ReadonlyArray<{ kind: string; number: number }>,
  kind: 'pr' | 'issue',
  n: number,
): boolean {
  return deferred.some((d) => d.kind === kind && d.number === n);
}

// The "Select a PR..." Details prompt is only satisfiable while PRs may
// still arrive (loading) or a visible PR can be selected; once loaded
// with zero visible PRs the pane would be permanently dead, so hide it.
export function detailsPromptVisible(prsLoaded: boolean, visiblePrCount: number): boolean {
  return !prsLoaded || visiblePrCount > 0;
}

// Two columns only when a PR detail pane is open (PrDetails). The
// "Select a PR..." prompt renders as a slim notice above the lists,
// so it never justifies the 320px rail (#599).
export function reviewColumnsClass(hasDetail: boolean): string {
  return hasDetail ? 'columns' : 'columns columns-single';
}

export function rowMatchesFilter(
  row: { number: number; title: string; labels?: unknown },
  query: string,
): boolean {
  const q = query.trim().replace(/^#/, '').toLowerCase();
  if (!q) return true;
  if (String(row.number).includes(q)) return true;
  if (row.title.toLowerCase().includes(q)) return true;
  const labels = Array.isArray(row.labels) ? row.labels : [];
  return labels.some((l) => labelOf(l).toLowerCase().includes(q));
}

// List sort (#817): API order (today's order, byte-identical) plus
// newest/oldest over the rows' updatedAt. The comparator is stable
// for rows without a parseable timestamp (compares equal), so PR
// rows — which lack updatedAt until the backend follow-up lands —
// hold API order either way. Order is never faked from numbers.
export type ReviewSort = 'api' | 'newest' | 'oldest';

export function parseReviewSort(value: string): ReviewSort {
  return value === 'newest' || value === 'oldest' ? value : 'api';
}

function rowUpdatedAt(row: unknown): number {
  if (!row || typeof row !== 'object') return NaN;
  const v = (row as Record<string, unknown>).updatedAt;
  if (typeof v !== 'string') return NaN;
  const t = Date.parse(v);
  return Number.isNaN(t) ? NaN : t;
}

export function compareReviewRows(a: unknown, b: unknown, sort: ReviewSort): number {
  if (sort === 'api') return 0;
  const ta = rowUpdatedAt(a);
  const tb = rowUpdatedAt(b);
  if (Number.isNaN(ta) || Number.isNaN(tb)) return 0;
  return sort === 'newest' ? tb - ta : ta - tb;
}

export function sortReviewRows<T>(rows: readonly T[], sort: ReviewSort): T[] {
  if (sort === 'api') return [...rows];
  return [...rows].sort((a, b) => compareReviewRows(a, b, sort));
}

function labelOf(v: unknown): string {
  if (typeof v === 'string') return v;
  if (v && typeof v === 'object') {
    const o = v as Record<string, unknown>;
    if (typeof o.login === 'string') return o.login;
    if (typeof o.name === 'string') return o.name;
  }
  return '';
}

// #938: 2-line risk summary mirror of backend build_risk_summary
// (backend/api/routers/pulls.py). Backend-provided riskSummary wins;
// this mirror covers older payloads and ?mock=1 fixtures so every
// card still shows a summary. Keep thresholds in sync both sides.
function countOf(v: unknown): number {
  return typeof v === 'number' && Number.isFinite(v) ? Math.max(0, v) : 0;
}

export type RiskLevel = 'low' | 'medium' | 'high';

export function buildRiskSummary(input: {
  title?: string;
  branch?: string;
  isDraft?: boolean;
  ciStatus?: string;
  reviewDecision?: string;
  mergeable?: string;
  additions?: number;
  deletions?: number;
  changedFiles?: number;
  conflicted?: boolean;
}): { summary: string; level: RiskLevel } {
  const title = input.title ?? '';
  const branch = input.branch ?? '';
  const additions = countOf(input.additions);
  const deletions = countOf(input.deletions);
  const changedFiles = countOf(input.changedFiles);
  const total = additions + deletions;
  const text = `${title} ${branch}`.toLowerCase();
  const dep = ['dependabot', 'bump', 'deps', 'dependency', 'dependencies'].some(
    (k) => text.includes(k),
  );
  const breaking = title.toLowerCase().includes('breaking') || title.includes('!:');
  const ci = (input.ciStatus || 'unknown').toLowerCase();
  const mergeableU = (input.mergeable || 'UNKNOWN').toUpperCase();
  const filesWord = changedFiles === 1 ? 'file' : 'files';
  let line1: string;
  if (changedFiles === 0 && total === 0) {
    line1 = 'No file changes detected · +0/-0';
  } else if (dep && total <= 200 && changedFiles <= 10) {
    line1 = `Minor dependency bump · ${changedFiles} ${filesWord} changed (+${additions}/-${deletions})`;
  } else if (dep) {
    line1 = `Dependency update · ${changedFiles} ${filesWord} changed (+${additions}/-${deletions})`;
  } else {
    const size = total <= 20 ? 'Small change' : total <= 150 ? 'Moderate change' : 'Large change';
    line1 = `${size} · ${changedFiles} ${filesWord} changed (+${additions}/-${deletions})`;
  }
  let line2: string;
  let level: RiskLevel;
  if (input.isDraft) {
    line2 = `Draft — not ready to merge; CI ${ci}`;
    level = 'high';
  } else if (ci === 'failing') {
    line2 = 'CI failing — fix checks before merging';
    level = 'high';
  } else if (ci !== 'passing') {
    line2 = `CI ${ci} — wait for checks before merging`;
    level = 'medium';
  } else if (input.conflicted || mergeableU === 'CONFLICTING') {
    line2 = 'Merge conflicts — resolve before merging';
    level = 'high';
  } else if (!['MERGEABLE', 'CLEAN', ''].includes(mergeableU)) {
    line2 = `Merge state ${mergeableU.toLowerCase()} — check before merging`;
    level = 'medium';
  } else if ((input.reviewDecision || '') === 'CHANGES_REQUESTED') {
    line2 = 'Changes requested — address review feedback';
    level = 'high';
  } else if (breaking) {
    line2 = 'Breaking change flagged — review carefully; CI green';
    level = 'high';
  } else if (changedFiles > 20 || total > 500) {
    line2 = 'Higher risk: large diff — review carefully; 0 breaking changes detected; CI green';
    level = 'high';
  } else if (changedFiles > 10 || total > 200) {
    line2 = 'Moderate risk — review diff before merging; 0 breaking changes detected; CI green';
    level = 'medium';
  } else {
    line2 = 'Low risk; 0 breaking changes detected; CI green';
    level = 'low';
  }
  return { summary: `${line1}\n${line2}`, level };
}

export function prRiskSummary(pr: PrRow): string {
  const provided = typeof pr.riskSummary === 'string' ? pr.riskSummary.trim() : '';
  if (provided && provided.includes('\n')) return provided;
  if (provided) return provided;
  return buildRiskSummary({
    title: pr.title,
    branch: pr.branch,
    isDraft: pr.isDraft,
    ciStatus: pr.ciStatus,
    reviewDecision: pr.reviewDecision,
    mergeable: pr.mergeable,
    additions: pr.additions,
    deletions: pr.deletions,
    changedFiles: pr.changedFiles,
  }).summary;
}

export function prRiskLevel(pr: PrRow): RiskLevel {
  if (pr.riskLevel === 'low' || pr.riskLevel === 'medium' || pr.riskLevel === 'high') {
    // A stale low from an older payload must not hide a draft/failing
    // state the row now carries: recompute when the summary fallback
    // disagrees on a blocking signal.
    const computed = buildRiskSummary({
      title: pr.title,
      branch: pr.branch,
      isDraft: pr.isDraft,
      ciStatus: pr.ciStatus,
      reviewDecision: pr.reviewDecision,
      mergeable: pr.mergeable,
      additions: pr.additions,
      deletions: pr.deletions,
      changedFiles: pr.changedFiles,
    });
    if (computed.level === 'high' && pr.isDraft) return 'high';
    if (computed.level === 'high' && (pr.ciStatus || '').toLowerCase() === 'failing') return 'high';
    return pr.riskLevel;
  }
  return buildRiskSummary({
    title: pr.title,
    branch: pr.branch,
    isDraft: pr.isDraft,
    ciStatus: pr.ciStatus,
    reviewDecision: pr.reviewDecision,
    mergeable: pr.mergeable,
    additions: pr.additions,
    deletions: pr.deletions,
    changedFiles: pr.changedFiles,
  }).level;
}

export function detailsRiskSummary(details: PrDetailsResponse): { summary: string; level: RiskLevel } {
  const provided = typeof details.risk_summary === 'string' ? details.risk_summary.trim() : '';
  if (provided) {
    const lvl = details.risk_level;
    if (lvl === 'low' || lvl === 'medium' || lvl === 'high') return { summary: provided, level: lvl };
    if (provided.includes('Higher risk') || provided.includes('failing') || provided.includes('Draft')) {
      return { summary: provided, level: 'high' };
    }
    if (provided.includes('Moderate risk') || provided.includes('wait for checks')) {
      return { summary: provided, level: 'medium' };
    }
    return { summary: provided, level: 'low' };
  }
  return buildRiskSummary({
    title: details.title,
    branch: details.branch,
    isDraft: details.draft,
    ciStatus: details.ci,
    mergeable: details.conflicted ? 'CONFLICTING' : details.merge === 'clean' ? 'MERGEABLE' : details.merge.toUpperCase(),
    conflicted: details.conflicted,
    additions: details.stats.additions,
    deletions: details.stats.deletions,
    changedFiles: details.stats.total_files,
  });
}

export function RiskSummary({ pr }: { pr: PrRow }) {
  const summary = prRiskSummary(pr);
  const level = prRiskLevel(pr);
  const [line1 = '', line2 = ''] = summary.split('\n');
  return (
    <div
      className={`risk-summary risk-${level}`}
      data-testid={`review-risk-summary-${pr.number}`}
      data-risk-level={level}
      role="note"
      aria-label={`Risk summary for PR #${pr.number}: ${line1}. ${line2}`}
    >
      <span className="risk-line">{line1}</span>
      <span className="risk-line">{line2}</span>
    </div>
  );
}

export function PrDiffBody({
  diff,
  pr,
  idPrefix = 'review-diff',
}: {
  diff: PrDiffResponse;
  pr: number;
  idPrefix?: string;
}) {
  return (
    <>
      {diff.truncated && (
        <p className="row-meta">Truncated to the first 30 KB.</p>
      )}
      {diff.hidden_structural && diff.hidden_structural.count > 0 && (
        <p
          className="row-meta"
          data-testid={`${idPrefix}-hidden-${pr}`}
          title={diff.hidden_structural.files.join(', ')}
        >
          +{diff.hidden_structural.count} hidden structural files
        </p>
      )}
      <div className="actions">
        <CopyButton text={diff.diff} testid={`${idPrefix}-copy-${pr}`} label="Copy diff" />
      </div>
      <pre className="row-meta" data-testid={`${idPrefix}-body-${pr}`}>
        {diff.diff || 'Empty diff.'}
      </pre>
    </>
  );
}

export function ReviewListEmpty({
  testid,
  filtering,
  emptyText,
  onClearFilter,
}: {
  testid: string;
  filtering: boolean;
  emptyText: string;
  onClearFilter: () => void;
}) {
  return (
    <p className="row-meta" data-testid={testid}>
      {filtering ? (
        <>
          No matches for this filter.{' '}
          <button type="button" data-testid={`${testid}-clear`} onClick={onClearFilter}>
            Clear filter
          </button>
        </>
      ) : (
        emptyText
      )}
    </p>
  );
}

export function PrDiff({
  repo,
  pr,
  variant = 'details',
}: {
  repo: string;
  pr: number;
  variant?: 'card' | 'details';
}) {
  const [diff, setDiff] = useState<PrDiffResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const prefix = variant === 'card' ? 'review-card-diff' : 'review-diff';
  const mock = useMock();

  async function fetchDiff() {
    try {
      setDiff(await fetchPrDiff(repo, pr));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function load() {
    if (diff || error || mock) return;
    await fetchDiff();
  }

  return (
    <details
      data-testid={`${prefix}-${pr}`}
      onToggle={(e) => {
        if ((e.target as HTMLDetailsElement).open) void load();
      }}
    >
      <summary className="details-title">{variant === 'card' ? 'Diff preview' : 'View diff'}</summary>
      {mock ? (
        <p className="row-meta" data-testid={`${prefix}-mock-${pr}`}>
          Diff preview unavailable in preview mode.
        </p>
      ) : error ? (
        <RetryableError
          testid={`${prefix}-error-${pr}`}
          message={`Couldn't load diff: ${error}`}
          retryTestid={`${prefix}-retry-${pr}`}
          onRetry={() => {
            setError(null);
            void fetchDiff();
          }}
        />
      ) : diff ? (
        <PrDiffBody diff={diff} pr={pr} idPrefix={prefix} />
      ) : (
        <p className="row-meta" role="status">Loading diff…</p>
      )}
    </details>
  );
}

// Merge-block decision context (issue #839 addendum): wherever the
// pane's Merge is disabled, the block inputs render inline — the
// head reason, failing check names, conflicts, draft — plus the
// likely cause when the state is ruleset-blocked with CI passing
// and nothing failing (an untriggered required check, e.g. path
// filters, or branch rules). The API reports triggered checks
// only, so missing checks are named as the likely cause with a
// link to the PR's authoritative list — never as observed fact.
export function MergeBlockDetail({
  details,
  head,
}: {
  details: PrDetailsResponse;
  head: string | null;
}) {
  const rulesetLike = details.merge === 'blocked' || details.merge === 'unstable';
  const blockedGreen =
    rulesetLike &&
    details.ci === 'passing' &&
    details.failing.length === 0 &&
    !details.conflicted &&
    !details.draft;
  const behind = details.merge === 'behind';
  if (!head && !blockedGreen && !behind) return null;
  return (
    <div data-testid="review-merge-block">
      {head && <p className="row-meta">{head}</p>}
      {details.failing.length > 0 && (
        <p className="row-meta" data-testid="review-merge-failing">
          Failing checks: {details.failing.join(', ')}.
        </p>
      )}
      {details.conflicted && (
        <p className="row-meta">Has merge conflicts — resolve them before merging.</p>
      )}
      {details.draft && <p className="row-meta">Still a draft.</p>}
      {behind && (
        <p className="row-meta">
          Branch is behind {details.base || 'the base'} — update the branch, then merge.
        </p>
      )}
      {blockedGreen && (
        <p className="row-meta" data-testid="review-merge-missing">
          Blocked with CI passing and no failing checks reported — a required check probably
          never ran (path filters) or branch rules (ruleset or branch protection) block the
          merge. See the required checks{' '}
          {details.url ? (
            <a href={details.url} target="_blank" rel="noreferrer">
              on the PR
            </a>
          ) : (
            'on the PR'
          )}
          .
        </p>
      )}
    </div>
  );
}

// Headings stay in order: h3 PR title under the view h2, h4 prompts under
// it. (The "Details" h3 only renders for the empty state.)
export function PrDetails({
  repo,
  details,
  mergeBlocked,
  mergeDisabled,
  onMerge,
  onAskAgent,
}: {
  repo: string;
  details: PrDetailsResponse;
  mergeBlocked: string | null;
  mergeDisabled: boolean;
  onMerge: (n: number) => void;
  onAskAgent: (seed: string) => void;
}) {
  const risk = detailsRiskSummary(details);
  const [riskLine1 = '', riskLine2 = ''] = risk.summary.split('\n');
  return (
    <div className="detail" data-testid="review-details">
      <h3>#{details.number} {details.title}</h3>
      <p className="row-meta">
        {details.branch} into {details.base} · CI {details.ci} · merge {details.merge} ·{' '}
        {details.stats.total_files} files (+{details.stats.additions}/-{details.stats.deletions})
        {details.url ? (
          <>
            {' · '}
            <a
              href={details.url}
              target="_blank"
              rel="noreferrer"
              data-testid={`open-github-pr-${details.number}`}
            >
              Open on GitHub ↗
            </a>
          </>
        ) : null}
      </p>
      <div
        className={`risk-summary risk-${risk.level}`}
        data-testid="review-details-risk"
        data-risk-level={risk.level}
        role="note"
        aria-label={`Risk summary for PR #${details.number}: ${riskLine1}. ${riskLine2}`}
      >
        <span className="risk-line">{riskLine1}</span>
        <span className="risk-line">{riskLine2}</span>
      </div>
      <ul data-testid="review-files">
        {details.files.map((f, i) => (
          <li key={f.path}>
            {f.path} (+{f.additions}/-{f.deletions}){' '}
            <CopyButton text={f.path} testid={`review-file-copy-${i}`} label="Copy path" />
          </li>
        ))}
      </ul>
      <h4>Review prompts</h4>
      <ul data-testid="review-prompts">
        {details.ai_prompts.map((p) => (
          <li key={p.id}>
            <strong>{p.label}:</strong> {p.prompt}{' '}
            <button
              type="button"
              className="btn-secondary"
              data-testid={`ask-prompt-${p.id}`}
              onClick={() =>
                onAskAgent(
                  `On PR #${details.number} (${details.branch} into ${details.base}): ${p.prompt}`,
                )
              }
            >
              Ask
            </button>
          </li>
        ))}
      </ul>
      <PrDiff repo={repo} pr={details.number} />
      <MergeBlockDetail details={details} head={mergeBlocked} />
      <div className="actions">
        <TapToFire
          label={`Merge PR #${details.number}`}
          confirm="This cannot be undone."
          disabled={mergeDisabled}
          onFire={() => onMerge(details.number)}
        />
        <button
          type="button"
          data-testid={`ask-agent-pr-${details.number}`}
          onClick={() =>
            onAskAgent(
              `PR #${details.number} ${details.title} (${repo}): ${details.branch} into ${details.base}, CI ${details.ci}, merge ${details.merge}.`,
            )
          }
        >
          Ask agent
        </button>
      </div>
    </div>
  );
}

// Inline row merge: tap-to-fire with the same finality confirm as the
// Details merge, gated on the row-level block reasons. Merge is
// irreversible, so no undo — the inline confirm is the guard.
export function PrRowMerge({
  pr,
  merging,
  onMerge,
}: {
  pr: PrRow;
  merging: boolean;
  onMerge: (n: number) => void;
}) {
  return (
    <TapToFire
      label="Merge"
      testid={`merge-pr-${pr.number}`}
      confirm="This cannot be undone."
      disabled={mergeBlockReason(pr) !== null || merging}
      onFire={() => onMerge(pr.number)}
    />
  );
}

// Request-changes body cap, mirroring REVIEW_BODY_MAX on the server.
const REQUEST_CHANGES_MAX = 2000;

// Inline validation for the request-changes body field: null when
// submittable, otherwise the message to show (server re-checks).
export function validateRequestChangesBody(body: string): string | null {
  if (!body.trim()) return 'Review feedback is required.';
  if (body.length > REQUEST_CHANGES_MAX) {
    return `Keep feedback under ${REQUEST_CHANGES_MAX} characters.`;
  }
  return null;
}

// One-tap approve chip. Reviews are reversible (a later review
// supersedes), so unlike Merge this fires on one tap with no inline
// confirm — the backend pr-approve confirm intent still applies.
export function PrRowApprove({
  pr,
  busy,
  onApprove,
  viewer,
}: {
  pr: PrRow;
  busy: boolean;
  onApprove: (n: number) => void;
  viewer?: string;
}) {
  const blocked = approveBlockReason(pr, viewer);
  // #848: an impossible approval (own PR) names its reason inline —
  // a disabled chip alone reads as broken, a live chip 500s.
  const showInline = isOwnPr(pr, viewer) && blocked !== null;
  return (
    <>
      <button
        type="button"
        data-testid={`approve-pr-${pr.number}`}
        aria-label={`Approve PR #${pr.number}`}
        title={blocked ?? undefined}
        disabled={blocked !== null || busy}
        onClick={() => onApprove(pr.number)}
      >
        Approve
      </button>
      {showInline && (
        <span className="row-meta" data-testid={`approve-self-note-${pr.number}`}>
          {blocked}
        </span>
      )}
    </>
  );
}

// Merge + approve row pair with the compound shortcut (#932): when
// CI is green and a missing approval is the only blocker, one
// hold-to-confirm Approve & Merge replaces the pair (Merge would
// sit disabled naming that same blocker). All other rows keep the
// separate buttons.
export function PrRowReviewActions({
  pr,
  viewer,
  merging,
  reviewing,
  onMerge,
  onApprove,
  onApproveMerge,
}: {
  pr: PrRow;
  viewer?: string;
  merging: boolean;
  reviewing: boolean;
  onMerge: (n: number) => void;
  onApprove: (n: number) => void;
  onApproveMerge: (n: number) => void;
}) {
  if (approveMergeEligible(pr, viewer)) {
    return (
      <TapToFire
        label="Approve & Merge"
        testid={`approve-merge-pr-${pr.number}`}
        confirm="This cannot be undone."
        disabled={merging || reviewing}
        onFire={() => onApproveMerge(pr.number)}
      />
    );
  }
  return (
    <>
      <PrRowMerge pr={pr} merging={merging} onMerge={onMerge} />
      <PrRowApprove pr={pr} busy={reviewing} onApprove={onApprove} viewer={viewer} />
    </>
  );
}

// Request-changes toggle chip: opens the inline body field below the
// row actions. Never row-disabled — any open PR can take feedback.
export function PrRowRequestChanges({
  pr,
  busy,
  expanded,
  onToggle,
}: {
  pr: PrRow;
  busy: boolean;
  expanded: boolean;
  onToggle: (n: number) => void;
}) {
  return (
    <button
      type="button"
      data-testid={`request-changes-pr-${pr.number}`}
      aria-label={`Request changes on PR #${pr.number}`}
      aria-expanded={expanded}
      disabled={busy}
      onClick={() => onToggle(pr.number)}
    >
      Request changes
    </button>
  );
}

// Static canned replies for the request-changes body field: the
// vocabulary of review asks is small, so a fixed chip row covers
// most of it with zero backend. Chips compose (append); the human
// still submits. Each text is short — even all combined stay well
// under REQUEST_CHANGES_MAX.
export interface ReviewReplyTemplate {
  label: string;
  text: string;
}

export const REVIEW_REPLY_TEMPLATES: ReadonlyArray<ReviewReplyTemplate> = [
  { label: 'Formatting', text: 'Please fix formatting (run the repo formatter) and re-push.' },
  { label: 'Tests missing', text: 'Please add tests covering this change.' },
  { label: 'Rebase', text: 'Please rebase on main and resolve any conflicts.' },
  { label: 'Nits only', text: 'Overall looks good — just the nits above.' },
  { label: 'Needs context', text: 'Please add context: what changed, why, and how it was tested.' },
  { label: 'Docs', text: 'Please update the docs or changelog for this change.' },
];

// Append a canned reply to the draft: blank line between paragraphs,
// surrounding whitespace trimmed so taps never stack double spaces.
// Callers re-run validateRequestChangesBody on the result and refuse
// overflow with the existing message (no new error shape).
export function appendReplyTemplate(draft: string, snippet: string): string {
  const base = draft.trim();
  const add = snippet.trim();
  if (!base) return add;
  if (!add) return base;
  return `${base}\n\n${add}`;
}

// Inline request-changes body field, rendered under the row actions
// while the chip is expanded. Stays open on submit failure (the
// endpoint error surfaces in the view banner) so the draft survives
// a retry; the parent closes it on success.
export function RequestChangesForm({
  n,
  busy,
  onSubmit,
  onCancel,
}: {
  n: number;
  busy: boolean;
  onSubmit: (body: string) => void;
  onCancel: () => void;
}) {
  const [draft, setDraft] = useState('');
  const [problem, setProblem] = useState<string | null>(null);
  const bodyRef = useRef<HTMLTextAreaElement | null>(null);
  function insertTemplate(text: string) {
    const next = appendReplyTemplate(draft, text);
    if (next.length > REQUEST_CHANGES_MAX) {
      setProblem(validateRequestChangesBody(next));
      bodyRef.current?.focus();
      return;
    }
    setProblem(null);
    setDraft(next);
    bodyRef.current?.focus();
  }
  return (
    <form
      data-testid={`request-changes-form-${n}`}
      onSubmit={(e) => {
        e.preventDefault();
        const invalid = validateRequestChangesBody(draft);
        if (invalid) {
          setProblem(invalid);
          return;
        }
        setProblem(null);
        onSubmit(draft);
      }}
    >
      <div
        className="chip-row"
        style={{ flexWrap: 'nowrap', overflowX: 'auto' }}
        role="group"
        aria-label="Canned replies"
        data-testid={`review-reply-chips-${n}`}
      >
        {REVIEW_REPLY_TEMPLATES.map((t) => (
          <button
            key={t.label}
            type="button"
            data-testid="review-reply-chip"
            aria-label={`Insert canned reply: ${t.label}`}
            title={t.text}
            onClick={() => insertTemplate(t.text)}
          >
            {t.label}
          </button>
        ))}
      </div>
      <label className="row-meta" htmlFor={`request-changes-body-${n}`}>
        What needs to change?{' '}
        <textarea
          id={`request-changes-body-${n}`}
          data-testid={`request-changes-body-${n}`}
          ref={bodyRef}
          value={draft}
          maxLength={REQUEST_CHANGES_MAX}
          onChange={(e) => setDraft(e.target.value)}
        />
      </label>
      {problem && (
        <p className="row-meta" role="alert" data-testid={`request-changes-error-${n}`}>
          {problem}
        </p>
      )}
      <div className="actions">
        <button type="submit" data-testid={`request-changes-submit-${n}`} disabled={busy}>
          {busy ? 'Sending…' : 'Send review'}
        </button>{' '}
        <button type="button" data-testid={`request-changes-cancel-${n}`} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

// One plain row action (Open/Defer/Restore). The accessible name
// carries kind + number so 50 repeated buttons stay distinct for
// screen-reader users; visible text stays the bare verb.
export function RowActionButton({
  action,
  kind,
  num,
  testid,
  onClick,
}: {
  action: 'Details' | 'Defer' | 'Restore' | 'Open';
  kind: 'pr' | 'issue';
  num: number;
  testid?: string;
  onClick: () => void;
}) {
  const what = kind === 'pr' ? 'PR' : 'issue';
  return (
    <button type="button" data-testid={testid} aria-label={`${action} ${what} #${num}`} onClick={onClick}>
      {action}
    </button>
  );
}

// Label names for an issue row: gh shapes them as {name} objects,
// fixtures and optimistic patches may use bare strings.
export function issueLabelNames(labels: IssueRow['labels']): string[] {
  return (labels ?? []).map(labelOf).filter(Boolean);
}

// Issues quick-filter tabs (#962): Bugs and Enhancements read the
// row labels; Needs Triage is unlabeled rows plus triage-flagged ones.
export type IssueTab = 'all' | 'bugs' | 'enhancements' | 'needs-triage';

export const ISSUE_TABS: Array<{ id: IssueTab; label: string }> = [
  { id: 'all', label: 'All' },
  { id: 'bugs', label: 'Bugs' },
  { id: 'enhancements', label: 'Enhancements' },
  { id: 'needs-triage', label: 'Needs Triage' },
];

export function issueInTab(issue: IssueRow, tab: IssueTab): boolean {
  if (tab === 'all') return true;
  const names = issueLabelNames(issue.labels);
  const text = names.join(' ').toLowerCase();
  if (tab === 'bugs') return /\bbug/.test(text);
  if (tab === 'enhancements') return text.includes('enhancement') || text.includes('feature');
  return names.length === 0 || /triage/i.test(text);
}

// Inline severity from labels (#962): critical/high/medium/low
// keywords, else null (no badge). Reuses the Now severity palette.
export type IssueSeverity = 'critical' | 'high' | 'medium' | 'low';

export function issueSeverity(labels: IssueRow['labels']): IssueSeverity | null {
  const text = issueLabelNames(labels).join(' ').toLowerCase();
  if (/\bcritical\b|\bp0\b|severity:\s*critical/.test(text)) return 'critical';
  if (/\bhigh\b|\bp1\b|severity:\s*high/.test(text)) return 'high';
  if (/\bmedium\b|\bp2\b|severity:\s*medium/.test(text)) return 'medium';
  if (/\blow\b|\bp3\b|severity:\s*low/.test(text)) return 'low';
  return null;
}

export function issueSeverityClass(sev: IssueSeverity | null): string {
  if (sev === 'critical') return 'sev sev-crit';
  if (sev === 'high' || sev === 'medium') return 'sev sev-warn';
  return 'sev sev-info';
}

// Per-row agent actions (#962): seeds for the agent drawer, same
// shape as the PR ask-agent seeds.
export function repairIssueSeed(repo: string, issue: IssueRow): string {
  return `In ${repo}, fix issue #${issue.number} (${issue.title}): investigate the cause, implement the fix, and verify with the repo's tests.`;
}

export function feedbackIssueSeed(repo: string, issue: IssueRow): string {
  return `In ${repo}, read the latest comments on issue #${issue.number} (${issue.title}) and address the feedback, then report what changed.`;
}

export function rebaseIssueSeed(repo: string, issue: IssueRow, branch: string): string {
  return `In ${repo}, rebase the working branch for issue #${issue.number} (${issue.title}) onto the latest ${branch} and resolve any conflicts.`;
}

// In-flight (or last failed) label op for one issue row.
export interface IssueLabelOp {
  op: 'add' | 'remove';
  label: string;
}

// Inline label editor for one issue row (#612): always-visible
// chips with one-tap remove, plus an Edit toggle opening the add
// form with repo-label typeahead. Server errors surface inline
// with retry; the parent refreshes the row on success. Accessible
// names carry kind + number like RowActionButton (#569).
//
// Readability cap (#1012): at most MAX_VISIBLE_LABELS chips render
// inline with a "+N more" expander; remove-button testids keep
// absolute indices so automation does not shift when collapsed.
// Suggestions are capped too — the native datalist already filters
// as you type, the cap only bounds the DOM.
const MAX_VISIBLE_LABELS = 3;
const MAX_LABEL_SUGGESTIONS = 100;

export function splitLabelNames(
  names: string[],
  expanded: boolean,
): { visible: Array<{ name: string; index: number }>; hiddenCount: number } {
  const indexed = names.map((name, index) => ({ name, index }));
  if (expanded || names.length <= MAX_VISIBLE_LABELS) {
    return { visible: indexed, hiddenCount: 0 };
  }
  return {
    visible: indexed.slice(0, MAX_VISIBLE_LABELS),
    hiddenCount: names.length - MAX_VISIBLE_LABELS,
  };
}
export function IssueRowLabels({
  issue,
  repoLabels,
  pending,
  error,
  initialOpen = false,
  onEnsureSuggestions,
  onAdd,
  onRemove,
  onRetry,
}: {
  issue: IssueRow;
  repoLabels: string[];
  pending: IssueLabelOp | null;
  error: string | null;
  initialOpen?: boolean;
  onEnsureSuggestions: () => void;
  onAdd: (label: string) => void;
  onRemove: (label: string) => void;
  onRetry: () => void;
}) {
  const [open, setOpen] = useState(initialOpen);
  const [draft, setDraft] = useState('');
  const [problem, setProblem] = useState<string | null>(null);
  const [labelsExpanded, setLabelsExpanded] = useState(false);
  const n = issue.number;
  const names = issueLabelNames(issue.labels);
  const busy = pending !== null;
  const { visible: visibleLabels, hiddenCount } = splitLabelNames(names, labelsExpanded);
  return (
    <>
      <p className="row-meta" data-testid={`issue-labels-${n}`}>
        {names.length === 0
          ? 'no labels'
          : visibleLabels.map(({ name, index: i }, vi) => (
              <span key={`${name}-${i}`}>
                {vi > 0 ? ', ' : ''}
                {name}{' '}
                <button
                  type="button"
                  data-testid={`label-remove-${n}-${i}`}
                  aria-label={`Remove label ${name} from issue #${n}`}
                  disabled={busy}
                  onClick={() => onRemove(name)}
                >
                  ×
                </button>
              </span>
            ))}
        {(hiddenCount > 0 || labelsExpanded) &&
          names.length > MAX_VISIBLE_LABELS && (
            <>
              {', '}
              <button
                type="button"
                data-testid={`labels-more-${n}`}
                aria-expanded={labelsExpanded}
                aria-label={
                  labelsExpanded
                    ? `Show fewer labels on issue #${n}`
                    : `Show ${hiddenCount} more labels on issue #${n}`
                }
                onClick={() => setLabelsExpanded((v) => !v)}
              >
                {labelsExpanded ? 'show less' : `+${hiddenCount} more`}
              </button>
            </>
          )}
      </p>
      <button
        type="button"
        data-testid={`edit-labels-issue-${n}`}
        aria-label={`Edit labels on issue #${n}`}
        aria-expanded={open}
        onClick={() => {
          if (!open) onEnsureSuggestions();
          setOpen(!open);
        }}
      >
        Edit labels
      </button>
      {open && (
        <form
          data-testid={`label-editor-${n}`}
          onSubmit={(e) => {
            e.preventDefault();
            const name = draft.trim();
            if (!name) {
              setProblem('Label name is required.');
              return;
            }
            setProblem(null);
            setDraft('');
            onAdd(name);
          }}
        >
          <input
            data-testid={`label-input-${n}`}
            aria-label={`Label name for issue #${n}`}
            value={draft}
            list={`label-suggestions-${n}`}
            placeholder="label name"
            onChange={(e) => setDraft(e.target.value)}
          />
          <datalist id={`label-suggestions-${n}`} data-testid={`label-suggestions-${n}`}>
            {repoLabels.slice(0, MAX_LABEL_SUGGESTIONS).map((name) => (
              <option key={name} value={name} />
            ))}
          </datalist>{' '}
          <button
            type="submit"
            data-testid={`label-add-${n}`}
            aria-label={`Add label to issue #${n}`}
            disabled={busy}
          >
            {pending?.op === 'add' ? 'Saving…' : 'Add'}
          </button>
        </form>
      )}
      {problem && (
        <p className="row-meta" role="alert" data-testid={`label-problem-${n}`}>
          {problem}
        </p>
      )}
      {error && (
        <p className="row-meta" role="alert" data-testid={`labels-error-${n}`}>
          {error}{' '}
          <button
            type="button"
            data-testid={`labels-retry-${n}`}
            aria-label={`Retry label change on issue #${n}`}
            disabled={busy}
            onClick={onRetry}
          >
            Retry
          </button>
        </p>
      )}
    </>
  );
}

// Bulk defer selection keys: "pr:12" / "issue:34" so one set spans
// both Review lists without kind collisions.
export function bulkRowKey(kind: 'pr' | 'issue', n: number): string {
  return `${kind}:${n}`;
}

export function parseBulkRowKey(key: string): { kind: 'pr' | 'issue'; number: number } | null {
  const parts = key.split(':');
  if (parts.length !== 2) return null;
  const [kind, num] = parts;
  const number = Number(num);
  if ((kind === 'pr' || kind === 'issue') && Number.isInteger(number) && number > 0) {
    return { kind, number };
  }
  return null;
}

// Validated rows for a bulk selection; malformed keys drop out.
export function parseBulkSelection(selected: ReadonlySet<string>): Array<{ kind: 'pr' | 'issue'; number: number }> {
  return [...selected]
    .map(parseBulkRowKey)
    .filter((r): r is { kind: 'pr' | 'issue'; number: number } => r !== null);
}

// Toggle one row checkbox without mutating the prior set.
export function toggleBulkSelected(
  selected: ReadonlySet<string>,
  key: string,
): ReadonlySet<string> {
  const next = new Set(selected);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  return next;
}

// Select-all toggle for one list: when every visible key is already
// selected, deselect the list; otherwise select the whole list. Keys
// from the other list pass through untouched.
export function toggleBulkSelectAll(
  selected: ReadonlySet<string>,
  keys: readonly string[],
): ReadonlySet<string> {
  const next = new Set(selected);
  const all = keys.length > 0 && keys.every((k) => next.has(k));
  if (all) {
    for (const k of keys) next.delete(k);
  } else {
    for (const k of keys) next.add(k);
  }
  return next;
}

export function bulkDeferNotice(count: number): string {
  return `Deferred ${count} row${count === 1 ? '' : 's'}.`;
}

// Bulk approve plan (#890): which selected PRs one Approve tap will
// approve, and which selected rows it skips with their reasons.
// Eligibility reuses approveBlockReason (draft / already-approved /
// self-authored), so the bar and the per-row chip can never
// disagree. Issue rows are PR-inapplicable; selected PR numbers
// missing from the loaded list skip rather than firing blind.
export interface BulkApprovePlan {
  eligible: number[];
  skipped: Array<{ number: number; reason: string }>;
  issueCount: number;
}

export function planBulkApprove(
  prs: readonly PrRow[],
  selected: ReadonlySet<string>,
  viewer?: string,
): BulkApprovePlan {
  const byNumber = new Map(prs.map((p) => [p.number, p]));
  const eligible: number[] = [];
  const skipped: Array<{ number: number; reason: string }> = [];
  let issueCount = 0;
  for (const row of parseBulkSelection(selected)) {
    if (row.kind === 'issue') {
      issueCount += 1;
      continue;
    }
    const pr = byNumber.get(row.number);
    if (!pr) {
      skipped.push({ number: row.number, reason: 'not in the current list' });
      continue;
    }
    const blocked = approveBlockReason(pr, viewer);
    if (blocked !== null) skipped.push({ number: row.number, reason: blocked });
    else eligible.push(row.number);
  }
  return { eligible, skipped, issueCount };
}

// Inline skip counts for the bulk bar: "2 skipped: already approved
// (1); is a draft (1)". Null when nothing skips, so the bar stays
// quiet on a clean selection.
export function bulkApproveSkippedNote(plan: BulkApprovePlan): string | null {
  const total = plan.skipped.length + plan.issueCount;
  if (total === 0) return null;
  const groups = new Map<string, number>();
  for (const s of plan.skipped) groups.set(s.reason, (groups.get(s.reason) ?? 0) + 1);
  const parts = [...groups].map(([reason, n]) => `${reason} (${n})`);
  if (plan.issueCount > 0) {
    parts.push(`issue rows are not approvable (${plan.issueCount})`);
  }
  return `${total} skipped: ${parts.join('; ')}`;
}

// Batch result notice: mirrors the single-row "Approved PR #n."
// when the batch is one PR, counts otherwise. Empty when nothing
// approved — the caller shows the per-PR errors instead.
export function bulkApproveNotice(approved: readonly number[], total: number): string {
  if (approved.length === 1 && total === 1) return `Approved PR #${approved[0]}.`;
  if (approved.length === 0) return '';
  if (approved.length === total) return `Approved ${total} PRs.`;
  return `Approved ${approved.length} of ${total} PRs.`;
}

// Per-PR errors for a partial (or total) bulk-approve failure:
// "PR #7: <msg>; PR #9: <msg>".
export function bulkApproveFailureText(
  failed: ReadonlyArray<{ number: number; error: string }>,
): string {
  return failed.map((f) => `PR #${f.number}: ${f.error}`).join('; ');
}

// Dependabot batch plan (#932): compound-eligible Dependabot PR
// numbers in ascending order. Empty when fewer than two qualify —
// a lone green bot just uses its row button.
export function planDependabotBatch(prs: readonly PrRow[], viewer?: string): number[] {
  const nums = prs
    .filter((p) => isDependabotPr(p) && approveMergeEligible(p, viewer))
    .map((p) => p.number)
    .sort((a, b) => a - b);
  return nums.length >= 2 ? nums : [];
}

export function dependabotBatchNotice(done: readonly number[], total: number): string {
  if (done.length === 1 && total === 1) return `Approved and merged PR #${done[0]}.`;
  if (done.length === 0) return '';
  if (done.length === total) return `Approved and merged ${total} Dependabot PRs.`;
  return `Approved and merged ${done.length} of ${total} Dependabot PRs.`;
}

export function DependabotBatchBar({
  count,
  busy,
  onFire,
}: {
  count: number;
  busy: boolean;
  onFire: () => void;
}) {
  return (
    <div className="actions">
      <button
        type="button"
        data-testid="batch-approve-merge-dependabot"
        disabled={busy}
        onClick={onFire}
      >
        {busy ? 'Approving & merging…' : `Batch Approve & Merge Dependabot (${count})`}
      </button>
    </div>
  );
}

// Fanout core (#890): approve each number through the single-row
// approve call, collecting per-PR outcomes. Sequential — N is small
// in phone triage, and ordering stays deterministic. Failures never
// abort the batch: successes stay approved.
export async function approveEach(
  approve: (n: number) => Promise<unknown>,
  numbers: readonly number[],
): Promise<{ approved: number[]; failed: Array<{ number: number; error: string }> }> {
  const approved: number[] = [];
  const failed: Array<{ number: number; error: string }> = [];
  for (const n of numbers) {
    try {
      await approve(n);
      approved.push(n);
    } catch (err) {
      failed.push({ number: n, error: err instanceof Error ? err.message : String(err) });
    }
  }
  return { approved, failed };
}

// Optimistic review-decision patch for N rows (bulk approve when the
// post-batch list re-read fails). Null-safe like dropMergedPr.
export function applyReviewDecision(
  prs: readonly PrRow[] | null,
  numbers: readonly number[],
  decision: string,
): PrRow[] | null {
  if (!prs) return prs;
  const set = new Set(numbers);
  return prs.map((r) => (set.has(r.number) ? { ...r, reviewDecision: decision } : r));
}

// PR↔issue linkage (issue #839): a PR links the issues it likely
// closes — explicit #N references in its title/branch plus
// same-title rows (the issue #4848 + PR #4849 filing pattern).
// Pure client-side over the loaded lists; chips only, no backend
// shape change. Only numbers present in the loaded issue list link.
export function prTitleRefs(text: string): number[] {
  const out: number[] = [];
  for (const m of text.matchAll(/#(\d+)/g)) {
    const n = Number(m[1]);
    if (Number.isInteger(n) && n > 0 && !out.includes(n)) out.push(n);
  }
  return out;
}

function normLinkTitle(t: string): string {
  return t.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
}

export function linkedIssueNumbers(pr: PrRow, issues: readonly IssueRow[]): number[] {
  const nums = new Set<number>();
  for (const n of [...prTitleRefs(pr.title), ...prTitleRefs(pr.branch ?? '')]) {
    if (issues.some((i) => i.number === n)) nums.add(n);
  }
  const want = normLinkTitle(pr.title);
  if (want) {
    for (const i of issues) {
      if (i.title && normLinkTitle(i.title) === want) nums.add(i.number);
    }
  }
  return [...nums].sort((a, b) => a - b);
}

export function linkedPrNumbers(issue: IssueRow, prs: readonly PrRow[]): number[] {
  const out: number[] = [];
  for (const p of prs) {
    if (linkedIssueNumbers(p, [issue]).length > 0) out.push(p.number);
  }
  return out.sort((a, b) => a - b);
}

export function bulkRestoreNotice(count: number): string {
  return `Restored ${count} row${count === 1 ? '' : 's'}.`;
}

// Bulk bar: rendered only while rows are selected. One Defer-n action
// over the whole selection plus (#890) one Approve-n action over the
// selected approvable PRs; single-row buttons keep their own shapes.
export function BulkDeferBar({
  count,
  busy,
  onDefer,
  approveCount,
  approveBusy = false,
  onApprove,
  skippedNote = null,
}: {
  count: number;
  busy: boolean;
  onDefer: () => void;
  // #890: absent (legacy callers) means no approve action at all.
  approveCount?: number;
  approveBusy?: boolean;
  onApprove?: () => void;
  skippedNote?: string | null;
}) {
  const approvable = approveCount ?? 0;
  const eitherBusy = busy || approveBusy;
  return (
    <div className="actions" data-testid="defer-bulk-bar" role="status">
      {count} selected{' '}
      <button
        type="button"
        data-testid="defer-bulk-defer"
        aria-label={`Defer ${count} selected row${count === 1 ? '' : 's'}`}
        disabled={eitherBusy}
        onClick={onDefer}
      >
        {busy ? 'Deferring…' : `Defer ${count}`}
      </button>
      {onApprove && (
        <button
          type="button"
          data-testid="defer-bulk-approve"
          aria-label={`Approve ${approvable} selected PR${approvable === 1 ? '' : 's'}`}
          title={approvable === 0 ? 'No selected PR is approvable' : undefined}
          disabled={eitherBusy || approvable === 0}
          onClick={onApprove}
        >
          {approveBusy ? 'Approving…' : `Approve ${approvable}`}
        </button>
      )}
      {skippedNote && (
        <span className="row-meta" data-testid="defer-bulk-approve-skipped">
          {skippedNote}
        </span>
      )}
    </div>
  );
}

// Bulk bar for the Deferred disclosure: same shape as BulkDeferBar,
// one Restore-n action. Separate selection from the defer bar so each
// count names only the rows its action applies to.
export function BulkRestoreBar({
  count,
  busy,
  onRestore,
}: {
  count: number;
  busy: boolean;
  onRestore: () => void;
}) {
  return (
    <div className="actions" data-testid="restore-bulk-bar" role="status">
      {count} selected{' '}
      <button
        type="button"
        data-testid="restore-bulk-restore"
        aria-label={`Restore ${count} selected row${count === 1 ? '' : 's'}`}
        disabled={busy}
        onClick={onRestore}
      >
        {busy ? 'Restoring…' : `Restore ${count}`}
      </button>
    </div>
  );
}

export default function ReviewView({
  repo,
  defaultBranch,
  onAskAgent,
  triageSuspended = false,
  onSelectPr,
  createRequest = 0,
}: {
  repo: string;
  // Checkout branch of the viewed repo: seeds the branch field
  // that merge/approve carry (#708).
  defaultBranch?: string;
  onAskAgent: (seed: string) => void;
  // Drawer-open flag: while true (agent drawer / PTY terminal open),
  // triage keys are disabled.
  triageSuspended?: boolean;
  // Reports row selection up so Now can prefill the Jobs PR field.
  onSelectPr: (n: number | null) => void;
  // Palette request (issue #839): a nonce bump opens the New-issue
  // form so creation is one command away, not a scroll away.
  createRequest?: number;
}) {
  const [prs, setPrs] = useState<PrRow[] | null>(null);
  // #848: gh login for self-approval checks; undefined until the PR
  // list loads (mocks never set it — Approve stays legacy there).
  const [viewer, setViewer] = useState<string | undefined>(undefined);
  const [branch, setBranch] = useBranchField(repo, defaultBranch);
  const [issues, setIssues] = useState<IssueRow[] | null>(null);
  const [deferred, setDeferred] = useState<DeferredRow[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [details, setDetails] = useState<PrDetailsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState('');
  const [draftCause, setDraftCause] = useState('');
  const [newTitle, setNewTitle] = useState('');
  const [newBody, setNewBody] = useState('');
  const [merging, setMerging] = useState<ReadonlySet<number>>(new Set());
  const [reviewing, setReviewing] = useState<ReadonlySet<number>>(new Set());
  const [requestChangesFor, setRequestChangesFor] = useState<number | null>(null);
  const [creatingIssue, setCreatingIssue] = useState(false);
  const [filter, setFilter] = useState('');
  // Issues quick-filter tab (#962): narrows the issue list (and the
  // triage cursor, which derives from it) by label family.
  const [issueTab, setIssueTab] = useState<IssueTab>('all');
  const [sort, setSort] = useState<ReviewSort>('api');
  const [bulkSelected, setBulkSelected] = useState<ReadonlySet<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
  // #890: bulk-approve in-flight flag, separate from bulkBusy (defer)
  // so each bar button keeps its own busy label.
  const [bulkApproveBusy, setBulkApproveBusy] = useState(false);
  // #932: dependabot batch in-flight flag, same pattern.
  const [depBotBusy, setDepBotBusy] = useState(false);
  const [bulkRestore, setBulkRestore] = useState<ReadonlySet<string>>(new Set());
  const [bulkRestoreBusy, setBulkRestoreBusy] = useState(false);
  // Inline issue labels (#612): repo labels load lazily on first
  // editor open; per-row pending/error/failed-op keyed by number.
  const [repoLabels, setRepoLabels] = useState<string[] | null>(null);
  const [labelPending, setLabelPending] = useState<Record<number, IssueLabelOp | null>>({});
  const [labelError, setLabelError] = useState<Record<number, string | null>>({});
  const [labelFailedOp, setLabelFailedOp] = useState<Record<number, IssueLabelOp | null>>({});
  // New-issue disclosure, controlled so the palette can open it.
  const [createOpen, setCreateOpen] = useState(false);
  const handledCreateNonce = useRef(0);
  useEffect(() => {
    if (createRequest !== handledCreateNonce.current) {
      handledCreateNonce.current = createRequest;
      if (createRequest > 0) setCreateOpen(true);
    }
  }, [createRequest]);

  // Deferred rows hide from the main lists (and from the triage
  // cursor) and surface in the Deferred disclosure instead. The
  // text filter then narrows what's left; the cursor derives from
  // the same narrowed lists, so j/k/d follow the filter.
  const visiblePrs = sortReviewRows(
    (prs ?? [])
      .filter((p) => !isRowDeferred(deferred, 'pr', p.number))
      .filter((p) => rowMatchesFilter(p, filter)),
    sort,
  );
  const issueTabCounts = (issues ?? []).filter(
    (i) => !isRowDeferred(deferred, 'issue', i.number),
  );
  const visibleIssues = sortReviewRows(
    issueTabCounts
      .filter((i) => rowMatchesFilter(i, filter))
      .filter((i) => issueInTab(i, issueTab)),
    sort,
  );
  const filtering = filter.trim().length > 0;
  const deferredKeys = deferred.map((d) => bulkRowKey(d.kind, d.number));
  // Bulk selection keys for the currently visible (filtered) rows;
  // select-all toggles exactly this set per list.
  const visiblePrKeys = visiblePrs.map((p) => bulkRowKey('pr', p.number));
  const visibleIssueKeys = visibleIssues.map((i) => bulkRowKey('issue', i.number));
  // #890: the bulk bar's Approve-n count + inline skip note derive
  // from the same selection the Defer-n action uses.
  const approvePlan = planBulkApprove(prs ?? [], bulkSelected, viewer);
  const approveSkippedNote = bulkApproveSkippedNote(approvePlan);
  // #932: dependabot batch targets derive from the visible list, so
  // the filter narrows the batch like it narrows select-all.
  const depBotPlan = planDependabotBatch(visiblePrs, viewer);

  // Keyboard triage cursor across PR rows then issue rows. Enter opens
  // a PR row in the details pane, an issue row in a new tab.
  // d defers the cursor row (reversible via Restore).
  const rows: Array<{ kind: 'pr' | 'issue'; number: number }> = [
    ...visiblePrs.map((p) => ({ kind: 'pr' as const, number: p.number })),
    ...visibleIssues.map((i) => ({ kind: 'issue' as const, number: i.number })),
  ];
  const [cursor, setCursor] = useTriageKeys({
    count: rows.length,
    suspended: triageSuspended,
    onOpen: (i) => {
      const r = rows[i];
      if (!r) return;
      if (r.kind === 'pr') {
        void selectPr(r.number);
        return;
      }
      openIssueUrl(visibleIssues.find((v) => v.number === r.number)?.url);
    },
    onDefer: (i) => {
      const r = rows[i];
      if (r) void deferSelected(r.kind, r.number);
    },
    // c seeds the agent from the cursor row (#962): the row's
    // repair seed for issues, the ask-agent seed for PRs.
    onAgent: (i) => {
      const r = rows[i];
      if (!r) return;
      if (r.kind === 'issue') {
        const iss = visibleIssues.find((v) => v.number === r.number);
        if (iss) onAskAgent(repairIssueSeed(repo, iss));
        return;
      }
      const p = visiblePrs.find((v) => v.number === r.number);
      if (p) {
        onAskAgent(
          `PR #${p.number} ${p.title} (${repo}): review the diff and implement the needed changes.`,
        );
      }
    },
    rowTestId: (i) => {
      const r = rows[i];
      return r.kind === 'pr' ? `review-pr-${r.number}` : `review-issue-${r.number}`;
    },
  });
  const prCount = visiblePrs.length;

  // List loader, extracted for the error-state retry (#907): a
  // generation guard drops late settles after repo switches, the
  // same protection the old per-effect cancelled flag gave.
  const loadGen = useRef(0);
  const loadLists = useCallback(async () => {
    if (useMock()) {
      setPrs(loadPrs());
      setIssues(loadIssues());
      return;
    }
    const gen = ++loadGen.current;
    setError(null);
    const live = () => gen === loadGen.current;
    const lists = (async () => {
      try {
        const [p, i] = await Promise.all([fetchPrs(repo), fetchIssues(repo)]);
        if (!live()) return;
        setPrs(p.prs);
        setViewer(p.viewer || undefined);
        setIssues(i.issues);
      } catch (err) {
        if (!live()) return;
        setError(err instanceof Error ? err.message : String(err));
      }
    })();
    // Deferred flags load independently: if the store is unreachable
    // the lists still render, with nothing hidden.
    const flags = (async () => {
      try {
        const d = await fetchDeferred(repo);
        if (!live()) return;
        setDeferred(d.deferred);
      } catch {
        if (!live()) return;
        setDeferred([]);
      }
    })();
    await Promise.all([lists, flags]);
  }, [repo]);

  useEffect(() => {
    setPrs(null);
    setViewer(undefined);
    setIssues(null);
    setDeferred([]);
    setSelected(null);
    setDetails(null);
    setError(null);
    setBulkSelected(new Set());
    void loadLists();
    return () => {
      loadGen.current++;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repo]);

  async function selectPr(n: number) {
    setSelected(n);
    onSelectPr(n);
    setDetails(null);
    if (useMock()) {
      const d = loadPrDetails();
      setDetails(n === d.number ? d : { ...d, number: n, title: `PR #${n}` });
      return;
    }
    try {
      setDetails(await fetchPrDetails(repo, n));
      // Silent next-item prefetch: warm the details+diff cache for the
      // next PR in list order so advancing renders instantly.
      const next = nextPrAfterMerge(
        visiblePrs.map((p) => p.number),
        n,
      );
      if (next !== null && next !== n) {
        void Promise.all([
          fetchPrDetails(repo, next),
          fetchPrDiff(repo, next),
        ]).catch(() => {});
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function deferSelected(kind: 'pr' | 'issue', n: number) {
    setError(null);
    if (isRowDeferred(deferred, kind, n)) return;
    if (useMock()) {
      setDeferred((prev) => [...prev, { kind, number: n, deferred_at: Date.now() / 1000 }]);
    } else {
      try {
        const r = await deferRow(repo, kind, n);
        setDeferred(r.deferred);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
        return;
      }
    }
    if (kind === 'pr' && selected === n) {
      setSelected(null);
      onSelectPr(null);
      setDetails(null);
    }
    // A singly-deferred row leaves the visible list; drop its checkbox
    // so the bulk count never names an already-deferred row.
    setBulkSelected((prev) => {
      const key = bulkRowKey(kind, n);
      if (!prev.has(key)) return prev;
      const next = new Set(prev);
      next.delete(key);
      return next;
    });
    setNotice(`Deferred ${kind === 'pr' ? 'PR' : 'issue'} #${n}.`);
  }

  // One request defers every checked row; the response hides them and
  // the selection clears. Pointer-path only — j/k/d still defer the
  // single cursor row via deferSelected.
  async function deferBulk() {
    const rows = parseBulkSelection(bulkSelected);
    if (rows.length === 0) return;
    setError(null);
    setBulkBusy(true);
    try {
      if (useMock()) {
        setDeferred((prev) => {
          const have = new Set(prev.map((d) => bulkRowKey(d.kind, d.number)));
          const fresh = rows.filter((r) => !have.has(bulkRowKey(r.kind, r.number)));
          return [
            ...prev,
            ...fresh.map((r) => ({ ...r, deferred_at: Date.now() / 1000 })),
          ];
        });
      } else {
        const r = await deferRowsBulk(repo, 'defer', rows);
        setDeferred(r.deferred);
      }
      if (selected !== null && rows.some((r) => r.kind === 'pr' && r.number === selected)) {
        setSelected(null);
        onSelectPr(null);
        setDetails(null);
      }
      setBulkSelected(new Set());
      setNotice(bulkDeferNotice(rows.length));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBulkBusy(false);
    }
  }

  async function restoreSelected(kind: 'pr' | 'issue', n: number) {
    setError(null);
    if (useMock()) {
      setDeferred((prev) => prev.filter((d) => !(d.kind === kind && d.number === n)));
    } else {
      try {
        const r = await restoreRow(repo, kind, n);
        setDeferred(r.deferred);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
        return;
      }
    }
    // A singly-restored row leaves the disclosure; drop its checkbox
    // so the bulk count never names an already-restored row.
    setBulkRestore((prev) => {
      const key = bulkRowKey(kind, n);
      if (!prev.has(key)) return prev;
      const next = new Set(prev);
      next.delete(key);
      return next;
    });
    setNotice(`Restored ${kind === 'pr' ? 'PR' : 'issue'} #${n}.`);
  }

  // One request restores every checked deferred row; the response
  // refreshes the disclosure and the selection clears. Mirrors
  // deferBulk; single-row Restore keeps its own shape.
  async function restoreBulk() {
    const rows = parseBulkSelection(bulkRestore);
    if (rows.length === 0) return;
    setError(null);
    setBulkRestoreBusy(true);
    try {
      if (useMock()) {
        const drop = new Set(rows.map((r) => bulkRowKey(r.kind, r.number)));
        setDeferred((prev) => prev.filter((d) => !drop.has(bulkRowKey(d.kind, d.number))));
      } else {
        const r = await deferRowsBulk(repo, 'restore', rows);
        setDeferred(r.deferred);
      }
      setBulkRestore(new Set());
      setNotice(bulkRestoreNotice(rows.length));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBulkRestoreBusy(false);
    }
  }

  async function onMerge(n: number, mergedNotice?: string) {
    setError(null);
    setMerging((prev) => applyOptimisticMerge(prev, n));
    try {
      await mergePr(repo, n, branch);
      setNotice(mergedNotice ?? `Merged PR #${n}.`);
      const numbers = visiblePrs.map((p) => p.number);
      const next = nextPrAfterMerge(numbers, n);
      // The merge landed: clear the optimistic flag and drop the row
      // from the list as part of the same transaction (#472), so the
      // merged PR never lingers with "Merging…" and disabled controls.
      setMerging((prev) => revertOptimisticMerge(prev, n));
      setPrs((prev) => dropMergedPr(prev, n));
      // A merged row leaves the visible list: clear selection like
      // deferSelected does so the details pane never shows a closed
      // PR with a re-enabled Merge button. The advance below
      // re-selects when another PR remains.
      if (selected === n) {
        setSelected(null);
        onSelectPr(null);
        setDetails(null);
      }
      // A merged row leaves the visible list; drop its checkbox so
      // the bulk count never names an already-merged row.
      setBulkSelected((prev) => {
        const key = bulkRowKey('pr', n);
        if (!prev.has(key)) return prev;
        const nextKeys = new Set(prev);
        nextKeys.delete(key);
        return nextKeys;
      });
      if (next !== null) {
        const at = numbers.filter((x) => x !== n).indexOf(next);
        if (at !== -1) setCursor(at);
        void selectPr(next);
      }
    } catch (err) {
      setMerging((prev) => revertOptimisticMerge(prev, n));
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  // Re-read the PR list after a posted review so the row's
  // reviewDecision refreshes without a manual reload. When the
  // re-read itself fails, patch the reviewed row optimistically —
  // the review already posted, so the decision is known.
  async function refreshPrs(n: number, decision: string) {
    try {
      const p = await fetchPrs(repo);
      setPrs(p.prs);
      setViewer(p.viewer || undefined);
    } catch {
      setPrs((prev) => applyReviewDecision(prev, [n], decision));
    }
  }

  // One tap approves every eligible selected PR (#890): the batch
  // fans out through the single-row approve endpoint per PR, so #852
  // gating and the backend 400 fallback apply unchanged. One list
  // re-read covers the batch; successes stay approved on partial
  // failure, and per-PR errors surface in the banner. Approved rows
  // stay listed (unlike deferred rows), so the selection is kept.
  async function approveBulk() {
    const plan = planBulkApprove(prs ?? [], bulkSelected, viewer);
    if (plan.eligible.length === 0) return;
    setError(null);
    setBulkApproveBusy(true);
    setReviewing((prev) => {
      const next = new Set(prev);
      for (const n of plan.eligible) next.add(n);
      return next;
    });
    try {
      const { approved, failed } = await approveEach(
        (n) => approvePr(repo, n, branch),
        plan.eligible,
      );
      if (approved.length > 0) {
        try {
          const p = await fetchPrs(repo);
          setPrs(p.prs);
          setViewer(p.viewer || undefined);
        } catch {
          setPrs((prev) => applyReviewDecision(prev, approved, 'APPROVED'));
        }
      }
      const notice = bulkApproveNotice(approved, plan.eligible.length);
      if (notice) setNotice(notice);
      if (failed.length > 0) setError(bulkApproveFailureText(failed));
    } finally {
      setReviewing((prev) => {
        const next = new Set(prev);
        for (const n of plan.eligible) next.delete(n);
        return next;
      });
      setBulkApproveBusy(false);
    }
  }

  async function onApprove(n: number) {
    setError(null);
    setReviewing((prev) => applyOptimisticMerge(prev, n));
    try {
      await approvePr(repo, n, branch);
      setNotice(`Approved PR #${n}.`);
      await refreshPrs(n, 'APPROVED');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setReviewing((prev) => revertOptimisticMerge(prev, n));
    }
  }

  // Compound Approve & Merge (#932): approve first, then ride the
  // full merge transaction (row removal, selection advance). An
  // approve failure stops before any merge; a merge failure keeps
  // the approval (the row re-read shows Merge enabled).
  async function onApproveMerge(n: number) {
    setError(null);
    setReviewing((prev) => applyOptimisticMerge(prev, n));
    try {
      await approvePr(repo, n, branch);
    } catch (err) {
      setReviewing((prev) => revertOptimisticMerge(prev, n));
      setError(err instanceof Error ? err.message : String(err));
      return;
    }
    setReviewing((prev) => revertOptimisticMerge(prev, n));
    await onMerge(n, `Approved and merged PR #${n}.`);
  }

  // Dependabot batch (#932): sequential approve+merge over the
  // planned numbers, one list re-read, one summary notice. Failures
  // never abort the batch; per-PR errors surface in the banner.
  async function approveMergeDependabots(numbers: readonly number[]) {
    if (numbers.length === 0) return;
    setError(null);
    setDepBotBusy(true);
    setReviewing((prev) => {
      const next = new Set(prev);
      for (const n of numbers) next.add(n);
      return next;
    });
    try {
      const { approved: done, failed } = await approveEach(async (n) => {
        await approvePr(repo, n, branch);
        await mergePr(repo, n, branch);
      }, numbers);
      if (done.length > 0) {
        try {
          const p = await fetchPrs(repo);
          setPrs(p.prs);
          setViewer(p.viewer || undefined);
        } catch {
          setPrs((prev) => {
            let next = prev;
            for (const n of done) next = dropMergedPr(next, n);
            return next;
          });
        }
        if (selected !== null && done.includes(selected)) {
          setSelected(null);
          onSelectPr(null);
          setDetails(null);
        }
      }
      const notice = dependabotBatchNotice(done, numbers.length);
      if (notice) setNotice(notice);
      if (failed.length > 0) setError(bulkApproveFailureText(failed));
    } finally {
      setReviewing((prev) => {
        const next = new Set(prev);
        for (const n of numbers) next.delete(n);
        return next;
      });
      setDepBotBusy(false);
    }
  }

  async function onRequestChanges(n: number, body: string) {
    setError(null);
    setReviewing((prev) => applyOptimisticMerge(prev, n));
    try {
      await requestPrChanges(repo, n, body);
      setRequestChangesFor(null);
      setNotice(`Requested changes on PR #${n}.`);
      await refreshPrs(n, 'CHANGES_REQUESTED');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setReviewing((prev) => revertOptimisticMerge(prev, n));
    }
  }

  // Lazily fetch the repo label list for editor typeahead.
  // Typeahead is an enhancement: the editor takes free text, so a
  // failed fetch degrades to no suggestions (cached, no refetch).
  async function ensureRepoLabels() {
    if (repoLabels !== null || useMock()) return;
    try {
      const r = await fetchRepoLabels(repo);
      setRepoLabels(r.labels);
    } catch {
      setRepoLabels([]);
    }
  }

  // One add/remove op on an issue row: the edit posts, then the
  // issue list re-reads so the row shows GitHub's own state (same
  // refresh shape as refreshPrs). Failures stay in the row with
  // retry; the view banner is untouched.
  async function runLabelOp(n: number, op: 'add' | 'remove', label: string) {
    setLabelError((prev) => ({ ...prev, [n]: null }));
    if (useMock()) {
      setIssues((prev) =>
        prev
          ? prev.map((r) => {
              if (r.number !== n) return r;
              const names = issueLabelNames(r.labels);
              const next =
                op === 'add'
                  ? names.includes(label)
                    ? names
                    : [...names, label]
                  : names.filter((x) => x !== label);
              return { ...r, labels: next.map((x) => ({ name: x })) };
            })
          : prev,
      );
      setNotice(`Updated labels on issue #${n}.`);
      return;
    }
    setLabelPending((prev) => ({ ...prev, [n]: { op, label } }));
    try {
      await editIssueLabel(repo, n, label, op);
      setLabelFailedOp((prev) => ({ ...prev, [n]: null }));
      const i = await fetchIssues(repo);
      setIssues(i.issues);
      setNotice(`Updated labels on issue #${n}.`);
    } catch (err) {
      setLabelFailedOp((prev) => ({ ...prev, [n]: { op, label } }));
      setLabelError((prev) => ({
        ...prev,
        [n]: err instanceof Error ? err.message : String(err),
      }));
    } finally {
      setLabelPending((prev) => ({ ...prev, [n]: null }));
    }
  }

  function retryLabelOp(n: number) {
    const failed = labelFailedOp[n];
    if (failed) void runLabelOp(n, failed.op, failed.label);
  }

  async function onCreateIssue() {
    setError(null);
    if (!newTitle.trim()) {
      setError('Issue title is required.');
      return;
    }
    setCreatingIssue(true);
    try {
      const r = await createIssue(repo, newTitle.trim(), newBody);
      setNotice(`Filed issue: ${r.url || 'created'}`);
      setNewTitle('');
      setNewBody('');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setCreatingIssue(false);
    }
  }

  async function onDraft() {
    setError(null);
    try {
      const d = await draftIssue(repo, draftTitle || 'Issue report from findings', draftCause, '');
      setNewTitle(d.title);
      setNewBody(d.body);
      setNotice('Draft loaded into the create-issue form.');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  const sel = details && selected !== null ? details : null;
  const promptVisible = detailsPromptVisible(prs !== null, visiblePrs.length);

  return (
    <section data-testid="review-view">
      <header className="page-head">
        <div>
          <h3>Review</h3>
        </div>
      </header>
      {error && (
        <RetryableError
          testid="review-error"
          message={error}
          retryTestid="review-retry"
          onRetry={() => void loadLists()}
        />
      )}
      {notice && (
        <p className="row-meta" data-testid="review-notice" role="status">{notice}</p>
      )}
      <TriageHint extra=" · d defers" />
      <div className="actions">
        <label className="row-meta" htmlFor="review-filter">
          Filter{' '}
          <input
            id="review-filter"
            data-testid="review-filter"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="number, title, or label"
          />
        </label>
        <BranchField
          id="review-branch"
          testid="review-branch"
          value={branch}
          onChange={setBranch}
        />
        <label className="row-meta" htmlFor="review-sort">
          Sort{' '}
          <select
            id="review-sort"
            data-testid="review-sort"
            value={sort}
            onChange={(e) => setSort(parseReviewSort(e.target.value))}
          >
            <option value="api">API order</option>
            <option value="newest">Newest first</option>
            <option value="oldest">Oldest first</option>
          </select>
        </label>
      </div>
      {bulkSelected.size > 0 && (
        <BulkDeferBar
          count={bulkSelected.size}
          busy={bulkBusy}
          onDefer={() => void deferBulk()}
          approveCount={approvePlan.eligible.length}
          approveBusy={bulkApproveBusy}
          onApprove={() => void approveBulk()}
          skippedNote={approveSkippedNote}
        />
      )}
      {!sel && promptVisible && (
        <p className="row-meta" data-testid="review-details-empty">
          Select a PR to see file stats and review prompts.
        </p>
      )}
      <div className={reviewColumnsClass(sel !== null)}>
        <div>
          <h3>
            Pull requests{' '}
            {visiblePrs.length > 0 && (
              <label className="row-meta">
                <input
                  type="checkbox"
                  data-testid="defer-select-all-prs"
                  aria-label="Select all pull requests"
                  checked={visiblePrKeys.every((k) => bulkSelected.has(k))}
                  onChange={() => setBulkSelected((prev) => toggleBulkSelectAll(prev, visiblePrKeys))}
                />{' '}
                all
              </label>
            )}
          </h3>
          {depBotPlan.length > 0 && (
            <DependabotBatchBar
              count={depBotPlan.length}
              busy={depBotBusy}
              onFire={() => void approveMergeDependabots(depBotPlan)}
            />
          )}
          {!prs ? (
            <p className="row-meta" data-testid="review-prs-loading" role="status">Loading pull requests…</p>
          ) : visiblePrs.length === 0 ? (
            <ReviewListEmpty
              testid="review-prs-empty"
              filtering={filtering}
              emptyText="No open PRs."
              onClearFilter={() => setFilter('')}
            />
          ) : (
            <ul className="session-list" data-testid="review-pr-list">
              {visiblePrs.map((p, pi) => (
                <li
                  key={p.number}
                  className={`detail${selected === p.number ? ' selected' : ''}${cursor === pi ? ' kbd-focus' : ''}${merging.has(p.number) ? ' merging' : ''}${reviewing.has(p.number) ? ' reviewing' : ''}`}
                  data-testid={`review-pr-${p.number}`}
                  aria-current={selected === p.number ? true : undefined}
                >
                  <input
                    type="checkbox"
                    data-testid={`defer-row-check-pr-${p.number}`}
                    aria-label={`Select PR #${p.number}`}
                    checked={bulkSelected.has(bulkRowKey('pr', p.number))}
                    onChange={() => setBulkSelected((prev) => toggleBulkSelected(prev, bulkRowKey('pr', p.number)))}
                  />{' '}
                  <span className="row-title">#{p.number} {p.title}</span>
                  <p className="row-meta">
                    {p.author} · {p.branch} · CI {p.ciLabel}
                    {p.isDraft ? ' · draft' : ''}
                    {merging.has(p.number) ? ' · Merging…' : ''}
                    {reviewing.has(p.number) ? ' · Reviewing…' : ''}
                    {' · '}
                    {mergeBadge(p)}
                  </p>
                  {linkedIssueNumbers(p, issues ?? []).length > 0 && (
                    <p className="row-meta">
                      Linked:{' '}
                      {linkedIssueNumbers(p, issues ?? []).map((n) => (
                        <button
                          key={n}
                          type="button"
                          className="repo-chip link-chip"
                          data-testid={`pr-link-issue-${p.number}-${n}`}
                          aria-label={`Filter to linked issue #${n}`}
                          title={`Linked issue #${n} — tap to filter`}
                          onClick={() => setFilter(`#${n}`)}
                        >
                          #{n}
                        </button>
                      ))}
                    </p>
                  )}
                  <RiskSummary pr={p} />
                  {/* Row verbs share one wrapped action row (#839):
                      as direct li children the full-row button rule
                      stacked them into five vertical text lines. */}
                  <div className="actions">
                    <RowActionButton action="Details" kind="pr" num={p.number} testid={`details-pr-${p.number}`} onClick={() => void selectPr(p.number)} />
                    <RowActionButton action="Defer" kind="pr" num={p.number} testid={`defer-pr-${p.number}`} onClick={() => void deferSelected('pr', p.number)} />
                    <PrRowReviewActions
                      pr={p}
                      viewer={viewer}
                      merging={merging.has(p.number)}
                      reviewing={reviewing.has(p.number)}
                      onMerge={(n) => void onMerge(n)}
                      onApprove={(n) => void onApprove(n)}
                      onApproveMerge={(n) => void onApproveMerge(n)}
                    />
                    <PrRowRequestChanges
                      pr={p}
                      busy={reviewing.has(p.number)}
                      expanded={requestChangesFor === p.number}
                      onToggle={(n) => setRequestChangesFor((prev) => (prev === n ? null : n))}
                    />
                  </div>
                  {requestChangesFor === p.number && (
                    <RequestChangesForm
                      n={p.number}
                      busy={reviewing.has(p.number)}
                      onSubmit={(body) => void onRequestChanges(p.number, body)}
                      onCancel={() => setRequestChangesFor(null)}
                    />
                  )}
                  <PrDiff repo={repo} pr={p.number} variant="card" />
                </li>
              ))}
            </ul>
          )}
          <h3>
            Issues{' '}
            {visibleIssues.length > 0 && (
              <label className="row-meta">
                <input
                  type="checkbox"
                  data-testid="defer-select-all-issues"
                  aria-label="Select all issues"
                  checked={visibleIssueKeys.every((k) => bulkSelected.has(k))}
                  onChange={() => setBulkSelected((prev) => toggleBulkSelectAll(prev, visibleIssueKeys))}
                />{' '}
                all
              </label>
            )}
          </h3>
          <div className="actions" data-testid="issue-filter-tabs" role="group" aria-label="Filter issues">
            {ISSUE_TABS.map((t) => (
              <button
                key={t.id}
                type="button"
                data-testid={`issue-tab-${t.id}`}
                aria-pressed={issueTab === t.id}
                onClick={() => setIssueTab(t.id)}
              >
                {t.label} ({issueTabCounts.filter((i) => issueInTab(i, t.id)).length})
              </button>
            ))}
            <span className="row-meta" data-testid="issue-triage-hint">
              j/k move · Enter/O opens · d/D defers · c asks agent
            </span>
          </div>
          {!issues ? (
            <p className="row-meta" data-testid="review-issues-loading" role="status">Loading issues…</p>
          ) : visibleIssues.length === 0 ? (
            <ReviewListEmpty
              testid="review-issues-empty"
              filtering={filtering}
              emptyText="No open issues."
              onClearFilter={() => setFilter('')}
            />
          ) : (
            <ul className="session-list" data-testid="review-issue-list">
              {visibleIssues.map((iss, ii) => {
                const sev = issueSeverity(iss.labels);
                return (
                <li
                  key={iss.number}
                  className={`detail${cursor === prCount + ii ? ' kbd-focus' : ''}`}
                  data-testid={`review-issue-${iss.number}`}
                >
                  <input
                    type="checkbox"
                    data-testid={`defer-row-check-issue-${iss.number}`}
                    aria-label={`Select issue #${iss.number}`}
                    checked={bulkSelected.has(bulkRowKey('issue', iss.number))}
                    onChange={() => setBulkSelected((prev) => toggleBulkSelected(prev, bulkRowKey('issue', iss.number)))}
                  />{' '}
                  <span className="row-title">
                    <IssueRowTitle issue={iss} />
                    {sev && (
                      <span
                        className={issueSeverityClass(sev)}
                        data-testid={`issue-severity-${iss.number}`}
                      >
                        {sev}
                      </span>
                    )}
                  </span>
                  <IssueRowLabels
                    issue={iss}
                    repoLabels={repoLabels ?? []}
                    pending={labelPending[iss.number] ?? null}
                    error={labelError[iss.number] ?? null}
                    onEnsureSuggestions={() => void ensureRepoLabels()}
                    onAdd={(label) => void runLabelOp(iss.number, 'add', label)}
                    onRemove={(label) => void runLabelOp(iss.number, 'remove', label)}
                    onRetry={() => retryLabelOp(iss.number)}
                  />
                  {linkedPrNumbers(iss, prs ?? []).length > 0 && (
                    <p className="row-meta">
                      Linked:{' '}
                      {linkedPrNumbers(iss, prs ?? []).map((n) => (
                        <button
                          key={n}
                          type="button"
                          className="repo-chip link-chip"
                          data-testid={`issue-link-pr-${iss.number}-${n}`}
                          aria-label={`Open linked PR #${n}`}
                          title={`Linked PR #${n} — tap to open`}
                          onClick={() => void selectPr(n)}
                        >
                          PR #{n}
                        </button>
                      ))}
                    </p>
                  )}
                  <div className="actions">
                    <RowActionButton action="Open" kind="issue" num={iss.number} testid={`open-issue-${iss.number}`} onClick={() => openIssueUrl(iss.url)} />
                    <RowActionButton action="Defer" kind="issue" num={iss.number} testid={`defer-issue-${iss.number}`} onClick={() => void deferSelected('issue', iss.number)} />
                    <button
                      type="button"
                      data-testid={`repair-issue-${iss.number}`}
                      title="Ask the agent to fix this issue"
                      onClick={() => onAskAgent(repairIssueSeed(repo, iss))}
                    >
                      Repair
                    </button>
                    <button
                      type="button"
                      data-testid={`feedback-issue-${iss.number}`}
                      title="Ask the agent to address the latest feedback on this issue"
                      onClick={() => onAskAgent(feedbackIssueSeed(repo, iss))}
                    >
                      Feedback
                    </button>
                    <button
                      type="button"
                      data-testid={`rebase-issue-${iss.number}`}
                      title="Ask the agent to rebase the working branch onto the latest base"
                      onClick={() => onAskAgent(rebaseIssueSeed(repo, iss, branch))}
                    >
                      Rebase
                    </button>
                  </div>
                </li>
                );
              })}
            </ul>
          )}
          {deferred.length > 0 && (
            <details data-testid="review-deferred-disclosure">
              <summary className="details-title">Deferred ({deferred.length})</summary>
              <label className="row-meta">
                <input
                  type="checkbox"
                  data-testid="restore-select-all"
                  aria-label="Select all deferred rows"
                  checked={deferredKeys.every((k) => bulkRestore.has(k))}
                  onChange={() => setBulkRestore((prev) => toggleBulkSelectAll(prev, deferredKeys))}
                />{' '}
                all
              </label>
              {bulkRestore.size > 0 && (
                <BulkRestoreBar count={bulkRestore.size} busy={bulkRestoreBusy} onRestore={() => void restoreBulk()} />
              )}
              <ul className="session-list" data-testid="review-deferred-list">
                {deferred.map((d) => {
                  const title = d.kind === 'pr'
                    ? (prs ?? []).find((p) => p.number === d.number)?.title
                    : (issues ?? []).find((i) => i.number === d.number)?.title;
                  return (
                    <li
                      key={`${d.kind}-${d.number}`}
                      data-testid={`review-deferred-${d.kind}-${d.number}`}
                    >
                      <input
                        type="checkbox"
                        data-testid={`restore-row-check-${d.kind}-${d.number}`}
                        aria-label={`Select ${d.kind} #${d.number}`}
                        checked={bulkRestore.has(bulkRowKey(d.kind, d.number))}
                        onChange={() => setBulkRestore((prev) => toggleBulkSelected(prev, bulkRowKey(d.kind, d.number)))}
                      />{' '}
                      <span className="row-title">#{d.number} {title ?? d.kind}</span>
                      <p className="row-meta">{d.kind === 'pr' ? 'pull request' : 'issue'}</p>
                      <RowActionButton action="Restore" kind={d.kind} num={d.number} testid={`restore-deferred-${d.kind}-${d.number}`} onClick={() => void restoreSelected(d.kind, d.number)} />
                    </li>
                  );
                })}
              </ul>
            </details>
          )}
        </div>
        <div>
          {sel && (
            <PrDetails
              repo={repo}
              details={sel}
              mergeBlocked={detailsMergeBlockReason(sel)}
              mergeDisabled={detailsMergeBlockReason(sel) !== null || merging.has(sel.number)}
              onMerge={(n) => void onMerge(n)}
              onAskAgent={onAskAgent}
            />
          )}
          <details
            data-testid="review-create-disclosure"
            open={createOpen}
            onToggle={(e) => {
              if (e.target === e.currentTarget) setCreateOpen(e.currentTarget.open);
            }}
          >
            <summary className="details-title">New issue</summary>
          <form
            data-testid="review-create-form"
            onSubmit={(e) => {
              e.preventDefault();
              void onCreateIssue();
            }}
          >
            <label className="row-meta" htmlFor="review-new-title">
              Title{' '}
              <input
                id="review-new-title"
                data-testid="review-new-title"
                value={newTitle}
                onChange={(e) => setNewTitle(e.target.value)}
              />
            </label>
            <label className="row-meta" htmlFor="review-new-body">
              Body{' '}
              <textarea
                id="review-new-body"
                data-testid="review-new-body"
                value={newBody}
                onChange={(e) => setNewBody(e.target.value)}
              />
            </label>
            <div className="actions">
              <button type="submit" disabled={creatingIssue}>
                {creatingIssue ? 'Creating…' : 'Create issue'}
              </button>
            </div>
          </form>
          </details>
          <details data-testid="review-draft-disclosure">
            <summary className="details-title">Draft from finding</summary>
          <div className="actions">
            <input
              data-testid="review-draft-title"
              aria-label="Draft title"
              placeholder="finding title"
              value={draftTitle}
              onChange={(e) => setDraftTitle(e.target.value)}
            />
            <input
              data-testid="review-draft-cause"
              aria-label="Draft cause"
              placeholder="likely cause"
              value={draftCause}
              onChange={(e) => setDraftCause(e.target.value)}
            />
            <button type="button" onClick={() => void onDraft()}>
              Draft issue
            </button>
          </div>
          </details>
        </div>
      </div>
    </section>
  );
}
