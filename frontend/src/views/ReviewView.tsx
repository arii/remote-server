import { useEffect, useState } from 'react';
import HoldToConfirm from '../components/HoldToConfirm';
import TriageHint from '../components/TriageHint';
import {
  createIssue,
  deferRow,
  draftIssue,
  fetchDeferred,
  fetchIssues,
  fetchPrDetails,
  fetchPrDiff,
  fetchPrs,
  mergePr,
  restoreRow,
} from '../apiClient';
import type { PrDiffResponse } from '../apiClient';
import type { DeferredRow, IssueRow, PrDetailsResponse, PrRow } from '../reviewTypes';
import { detailsMergeBlockReason } from '../reviewTypes';
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
      {mergeBlocked && (
        <p className="row-meta" data-testid="review-merge-block">
          {mergeBlocked}
        </p>
      )}
      <div className="actions">
        <HoldToConfirm
          label={`Merge PR #${details.number}`}
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

export default function ReviewView({
  repo,
  onAskAgent,
  triageSuspended = false,
}: {
  repo: string;
  onAskAgent: (seed: string) => void;
  // Drawer-open flag: while true (agent drawer / PTY terminal open),
  // triage keys are disabled.
  triageSuspended?: boolean;
}) {
  const [prs, setPrs] = useState<PrRow[] | null>(null);
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
  const [creatingIssue, setCreatingIssue] = useState(false);

  // Deferred rows hide from the main lists (and from the triage
  // cursor) and surface in the Deferred disclosure instead.
  const visiblePrs = (prs ?? []).filter(
    (p) => !isRowDeferred(deferred, 'pr', p.number),
  );
  const visibleIssues = (issues ?? []).filter(
    (i) => !isRowDeferred(deferred, 'issue', i.number),
  );

  // Keyboard triage cursor across PR rows then issue rows. Enter opens
  // a PR row; issue rows have no detail view, so Enter is a no-op there.
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
      if (r && r.kind === 'pr') void selectPr(r.number);
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
    setIssues(null);
    setDeferred([]);
    setSelected(null);
    setDetails(null);
    setError(null);
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
      setDetails(null);
    }
    setNotice(`Deferred ${kind === 'pr' ? 'PR' : 'issue'} #${n}.`);
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
    setNotice(`Restored ${kind === 'pr' ? 'PR' : 'issue'} #${n}.`);
  }

  async function onMerge(n: number) {
    setError(null);
    setMerging((prev) => applyOptimisticMerge(prev, n));
    try {
      await mergePr(repo, n);
      setNotice(`Merged PR #${n}.`);
      const numbers = visiblePrs.map((p) => p.number);
      const next = nextPrAfterMerge(numbers, n);
      if (next !== null) {
        const at = numbers.indexOf(next);
        if (at !== -1) setCursor(at);
        void selectPr(next);
      }
    } catch (err) {
      setMerging((prev) => revertOptimisticMerge(prev, n));
      setError(err instanceof Error ? err.message : String(err));
    }
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
      <div className="columns">
        <div>
          <h3>Pull requests</h3>
          {!prs ? (
            <p className="row-meta" data-testid="review-prs-loading" role="status">Loading…</p>
          ) : visiblePrs.length === 0 ? (
            <p className="row-meta" data-testid="review-prs-empty">No open PRs.</p>
          ) : (
            <ul className="session-list" data-testid="review-pr-list">
              {visiblePrs.map((p, pi) => (
                <li
                  key={p.number}
                  className={`detail${selected === p.number ? ' selected' : ''}${cursor === pi ? ' kbd-focus' : ''}${merging.has(p.number) ? ' merging' : ''}`}
                  data-testid={`review-pr-${p.number}`}
                  aria-current={selected === p.number ? true : undefined}
                >
                  <span className="row-title">#{p.number} {p.title}</span>
                  <p className="row-meta">
                    {p.author} · {p.branch} · CI {p.ciLabel}
                    {p.isDraft ? ' · draft' : ''}
                    {merging.has(p.number) ? ' · Merging…' : ''}
                  </p>
                  <button type="button" onClick={() => void selectPr(p.number)}>
                    Open
                  </button>{' '}
                  <button type="button" data-testid={`defer-pr-${p.number}`} onClick={() => void deferSelected('pr', p.number)}>
                    Defer
                  </button>
                </li>
              ))}
            </ul>
          )}
          <h3>Issues</h3>
          {!issues ? (
            <p className="row-meta" data-testid="review-issues-loading" role="status">Loading…</p>
          ) : visibleIssues.length === 0 ? (
            <p className="row-meta" data-testid="review-issues-empty">No open issues.</p>
          ) : (
            <ul className="session-list" data-testid="review-issue-list">
              {visibleIssues.map((iss, ii) => (
                <li
                  key={iss.number}
                  className={`detail${cursor === prCount + ii ? ' kbd-focus' : ''}`}
                  data-testid={`review-issue-${iss.number}`}
                >
                  <span className="row-title">#{iss.number} {iss.title}</span>
                  <p className="row-meta">
                    {(iss.labels || []).map(labelOf).filter(Boolean).join(', ') || 'no labels'}
                  </p>
                  <button type="button" data-testid={`defer-issue-${iss.number}`} onClick={() => void deferSelected('issue', iss.number)}>
                    Defer
                  </button>
                </li>
              ))}
            </ul>
          )}
          {deferred.length > 0 && (
            <details data-testid="review-deferred-disclosure">
              <summary className="details-title">Deferred ({deferred.length})</summary>
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
                      <span className="row-title">#{d.number} {title ?? d.kind}</span>
                      <p className="row-meta">{d.kind === 'pr' ? 'pull request' : 'issue'}</p>
                      <button
                        type="button"
                        data-testid={`restore-deferred-${d.kind}-${d.number}`}
                        onClick={() => void restoreSelected(d.kind, d.number)}
                      >
                        Restore
                      </button>
                    </li>
                  );
                })}
              </ul>
            </details>
          )}
        </div>
        <div>
          {!sel && promptVisible && (
            <>
              <h3>Details</h3>
              <p className="row-meta" data-testid="review-details-empty">
                Select a PR to see file stats and review prompts.
              </p>
            </>
          )}
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
          <details data-testid="review-create-disclosure">
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
