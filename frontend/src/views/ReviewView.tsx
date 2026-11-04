import { useEffect, useRef, useState } from 'react';
import BranchField, { useBranchField } from '../components/BranchField';
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
import { approveBlockReason, detailsMergeBlockReason, isOwnPr, mergeBlockReason } from '../reviewTypes';
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

function PrDiff({ repo, pr }: { repo: string; pr: number }) {
  const [diff, setDiff] = useState<PrDiffResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    if (diff || error || useMock()) return;
    try {
      setDiff(await fetchPrDiff(repo, pr));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <details
      data-testid={`review-diff-${pr}`}
      onToggle={(e) => {
        if ((e.target as HTMLDetailsElement).open) void load();
      }}
    >
      <summary className="details-title">View diff</summary>
      {error && (
        <p className="row-meta" role="alert">
          Couldn&apos;t load diff: {error}
        </p>
      )}
      {diff ? (
        <>
          {diff.truncated && (
            <p className="row-meta">Truncated to the first 30 KB.</p>
          )}
          {diff.hidden_structural && diff.hidden_structural.count > 0 && (
            <p
              className="row-meta"
              data-testid={`review-diff-hidden-${pr}`}
              title={diff.hidden_structural.files.join(', ')}
            >
              +{diff.hidden_structural.count} hidden structural files
            </p>
          )}
          <pre className="row-meta" data-testid={`review-diff-body-${pr}`}>
            {diff.diff || 'Empty diff.'}
          </pre>
        </>
      ) : (
        !error && <p className="row-meta" role="status">Loading…</p>
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
  return (
    <div className="detail" data-testid="review-details">
      <h3>#{details.number} {details.title}</h3>
      <p className="row-meta">
        {details.branch} into {details.base} · CI {details.ci} · merge {details.merge} ·{' '}
        {details.stats.total_files} files (+{details.stats.additions}/-{details.stats.deletions})
      </p>
      <ul data-testid="review-files">
        {details.files.map((f) => (
          <li key={f.path}>
            {f.path} (+{f.additions}/-{f.deletions})
          </li>
        ))}
      </ul>
      <h4>Review prompts</h4>
      <ul data-testid="review-prompts">
        {details.ai_prompts.map((p) => (
          <li key={p.id}>
            <strong>{p.label}:</strong> {p.prompt}
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
export const REQUEST_CHANGES_MAX = 2000;

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
  action: 'Open' | 'Defer' | 'Restore';
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
  const n = issue.number;
  const names = issueLabelNames(issue.labels);
  const busy = pending !== null;
  return (
    <>
      <p className="row-meta" data-testid={`issue-labels-${n}`}>
        {names.length === 0
          ? 'no labels'
          : names.map((name, i) => (
              <span key={`${name}-${i}`}>
                {i > 0 ? ', ' : ''}
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
            {repoLabels.map((name) => (
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

// Bulk bar: rendered only while rows are selected. One Defer-n action;
// single-row Defer/Restore buttons keep their own shapes.
export function BulkDeferBar({
  count,
  busy,
  onDefer,
}: {
  count: number;
  busy: boolean;
  onDefer: () => void;
}) {
  return (
    <div className="actions" data-testid="defer-bulk-bar" role="status">
      {count} selected{' '}
      <button
        type="button"
        data-testid="defer-bulk-defer"
        aria-label={`Defer ${count} selected row${count === 1 ? '' : 's'}`}
        disabled={busy}
        onClick={onDefer}
      >
        {busy ? 'Deferring…' : `Defer ${count}`}
      </button>
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
  const [sort, setSort] = useState<ReviewSort>('api');
  const [bulkSelected, setBulkSelected] = useState<ReadonlySet<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
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
  const visibleIssues = sortReviewRows(
    (issues ?? [])
      .filter((i) => !isRowDeferred(deferred, 'issue', i.number))
      .filter((i) => rowMatchesFilter(i, filter)),
    sort,
  );
  const filtering = filter.trim().length > 0;
  const deferredKeys = deferred.map((d) => bulkRowKey(d.kind, d.number));
  // Bulk selection keys for the currently visible (filtered) rows;
  // select-all toggles exactly this set per list.
  const visiblePrKeys = visiblePrs.map((p) => bulkRowKey('pr', p.number));
  const visibleIssueKeys = visibleIssues.map((i) => bulkRowKey('issue', i.number));

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
    rowTestId: (i) => {
      const r = rows[i];
      return r.kind === 'pr' ? `review-pr-${r.number}` : `review-issue-${r.number}`;
    },
  });
  const prCount = visiblePrs.length;

  useEffect(() => {
    let cancelled = false;
    setPrs(null);
    setViewer(undefined);
    setIssues(null);
    setDeferred([]);
    setSelected(null);
    setDetails(null);
    setError(null);
    setBulkSelected(new Set());
    if (useMock()) {
      setPrs(loadPrs());
      setIssues(loadIssues());
      return;
    }
    (async () => {
      try {
        const [p, i] = await Promise.all([fetchPrs(repo), fetchIssues(repo)]);
        if (!cancelled) {
          setPrs(p.prs);
          setViewer(p.viewer || undefined);
          setIssues(i.issues);
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
    })();
    // Deferred flags load independently: if the store is unreachable
    // the lists still render, with nothing hidden.
    (async () => {
      try {
        const d = await fetchDeferred(repo);
        if (!cancelled) setDeferred(d.deferred);
      } catch {
        if (!cancelled) setDeferred([]);
      }
    })();
    return () => {
      cancelled = true;
    };
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

  async function onMerge(n: number) {
    setError(null);
    setMerging((prev) => applyOptimisticMerge(prev, n));
    try {
      await mergePr(repo, n, branch);
      setNotice(`Merged PR #${n}.`);
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
      setPrs((prev) =>
        prev ? prev.map((r) => (r.number === n ? { ...r, reviewDecision: decision } : r)) : prev,
      );
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
        <p role="alert" data-testid="review-error">{error}</p>
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
        <BulkDeferBar count={bulkSelected.size} busy={bulkBusy} onDefer={() => void deferBulk()} />
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
          {!prs ? (
            <p className="row-meta" data-testid="review-prs-loading" role="status">Loading…</p>
          ) : visiblePrs.length === 0 ? (
            <p className="row-meta" data-testid="review-prs-empty">
              {filtering ? 'No matching PRs.' : 'No open PRs.'}
            </p>
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
                  {/* Row verbs share one wrapped action row (#839):
                      as direct li children the full-row button rule
                      stacked them into five vertical text lines. */}
                  <div className="actions">
                    <RowActionButton action="Open" kind="pr" num={p.number} testid={`open-pr-${p.number}`} onClick={() => void selectPr(p.number)} />
                    <RowActionButton action="Defer" kind="pr" num={p.number} testid={`defer-pr-${p.number}`} onClick={() => void deferSelected('pr', p.number)} />
                    <PrRowMerge pr={p} merging={merging.has(p.number)} onMerge={(n) => void onMerge(n)} />
                    <PrRowApprove pr={p} busy={reviewing.has(p.number)} onApprove={(n) => void onApprove(n)} viewer={viewer} />
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
          {!issues ? (
            <p className="row-meta" data-testid="review-issues-loading" role="status">Loading…</p>
          ) : visibleIssues.length === 0 ? (
            <p className="row-meta" data-testid="review-issues-empty">
              {filtering ? 'No matching issues.' : 'No open issues.'}
            </p>
          ) : (
            <ul className="session-list" data-testid="review-issue-list">
              {visibleIssues.map((iss, ii) => (
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
                  <RowActionButton action="Defer" kind="issue" num={iss.number} testid={`defer-issue-${iss.number}`} onClick={() => void deferSelected('issue', iss.number)} />
                </li>
              ))}
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
