import { useEffect, useState } from 'react';
import HoldToConfirm from '../components/HoldToConfirm';
import {
  createIssue,
  draftIssue,
  fetchIssues,
  fetchPrDetails,
  fetchPrDiff,
  fetchPrs,
  mergePr,
} from '../apiClient';
import type { PrDiffResponse } from '../apiClient';
import type { IssueRow, PrDetailsResponse, PrRow } from '../reviewTypes';
import { detailsMergeBlockReason } from '../reviewTypes';
import { TRIAGE_HINT, useTriageKeys } from '../triageKeys';
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
          <pre className="row-meta" data-testid={`review-diff-body-${pr}`}>
            {diff.diff || 'Empty diff.'}
          </pre>
        </>
      ) : (
        !error && <p className="row-meta">Loading…</p>
      )}
    </details>
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

  // Keyboard triage cursor across PR rows then issue rows. Enter opens
  // a PR row; issue rows have no detail view, so Enter is a no-op there.
  const rows: Array<{ kind: 'pr' | 'issue'; number: number }> = [
    ...(prs ?? []).map((p) => ({ kind: 'pr' as const, number: p.number })),
    ...(issues ?? []).map((i) => ({ kind: 'issue' as const, number: i.number })),
  ];
  const [cursor, setCursor] = useTriageKeys({
    count: rows.length,
    suspended: triageSuspended,
    onOpen: (i) => {
      const r = rows[i];
      if (r && r.kind === 'pr') void selectPr(r.number);
    },
    rowTestId: (i) => {
      const r = rows[i];
      return r.kind === 'pr' ? `review-pr-${r.number}` : `review-issue-${r.number}`;
    },
  });
  const prCount = prs?.length ?? 0;

  useEffect(() => {
    let cancelled = false;
    setPrs(null);
    setIssues(null);
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
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function onMerge(n: number) {
    setError(null);
    setMerging((prev) => applyOptimisticMerge(prev, n));
    try {
      await mergePr(repo, n);
      setNotice(`Merged PR #${n}.`);
      const numbers = (prs ?? []).map((p) => p.number);
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

  return (
    <section data-testid="review-view">
      <header className="page-head">
        <div>
          <h2>Review</h2>
          <p className="row-meta" data-testid="review-repo">Repo: {repo}</p>
        </div>
      </header>
      {error && (
        <p role="alert" data-testid="review-error">{error}</p>
      )}
      {notice && (
        <p className="row-meta" data-testid="review-notice">{notice}</p>
      )}
      <p className="row-meta" data-testid="triage-hint">{TRIAGE_HINT}</p>
      <div className="columns">
        <div>
          <h3>Pull requests</h3>
          {!prs ? (
            <p className="row-meta" data-testid="review-prs-loading">Loading…</p>
          ) : prs.length === 0 ? (
            <p className="row-meta" data-testid="review-prs-empty">No open PRs.</p>
          ) : (
            <ul className="session-list" data-testid="review-pr-list">
              {prs.map((p, pi) => (
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
                  </button>
                </li>
              ))}
            </ul>
          )}
          <h3>Issues</h3>
          {!issues ? (
            <p className="row-meta" data-testid="review-issues-loading">Loading…</p>
          ) : issues.length === 0 ? (
            <p className="row-meta" data-testid="review-issues-empty">No open issues.</p>
          ) : (
            <ul className="session-list" data-testid="review-issue-list">
              {issues.map((iss, ii) => (
                <li
                  key={iss.number}
                  className={`detail${cursor === prCount + ii ? ' kbd-focus' : ''}`}
                  data-testid={`review-issue-${iss.number}`}
                >
                  <span className="row-title">#{iss.number} {iss.title}</span>
                  <p className="row-meta">
                    {(iss.labels || []).map(labelOf).filter(Boolean).join(', ') || 'no labels'}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div>
          <h3>Details</h3>
          {!sel ? (
            <p className="row-meta" data-testid="review-details-empty">
              Select a PR to see file stats and review prompts.
            </p>
          ) : (
            <div className="detail" data-testid="review-details">
              <h2>#{sel.number} {sel.title}</h2>
              <p className="row-meta">
                {sel.branch} into {sel.base} · CI {sel.ci} · merge {sel.merge} ·{' '}
                {sel.stats.total_files} files (+{sel.stats.additions}/-{sel.stats.deletions})
              </p>
              <ul data-testid="review-files">
                {sel.files.map((f) => (
                  <li key={f.path}>
                    {f.path} (+{f.additions}/-{f.deletions})
                  </li>
                ))}
              </ul>
              <h4>Review prompts</h4>
              <ul data-testid="review-prompts">
                {sel.ai_prompts.map((p) => (
                  <li key={p.id}>
                    <strong>{p.label}:</strong> {p.prompt}
                  </li>
                ))}
              </ul>
              <PrDiff repo={repo} pr={sel.number} />
              {detailsMergeBlockReason(sel) && (
                <p className="row-meta" data-testid="review-merge-block">
                  {detailsMergeBlockReason(sel)}
                </p>
              )}
              <div className="actions">
                <HoldToConfirm
                  label={`Merge PR #${sel.number}`}
                  disabled={detailsMergeBlockReason(sel) !== null || merging.has(sel.number)}
                  onFire={() => void onMerge(sel.number)}
                />
                <button
                  type="button"
                  data-testid={`ask-agent-pr-${sel.number}`}
                  onClick={() =>
                    onAskAgent(
                      `PR #${sel.number} ${sel.title} (${repo}): ${sel.branch} into ${sel.base}, CI ${sel.ci}, merge ${sel.merge}.`,
                    )
                  }
                >
                  Ask agent
                </button>
              </div>
            </div>
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
