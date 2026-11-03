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
import { loadIssues, loadPrDetails, loadPrs, useMock } from './mockViews';

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
}: {
  repo: string;
  onAskAgent: (seed: string) => void;
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
    try {
      await mergePr(repo, n);
      setNotice(`Merged PR #${n}.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function onCreateIssue() {
    setError(null);
    if (!newTitle.trim()) {
      setError('Issue title is required.');
      return;
    }
    try {
      const r = await createIssue(repo, newTitle.trim(), newBody);
      setNotice(`Filed issue: ${r.url || 'created'}`);
      setNewTitle('');
      setNewBody('');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
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
      <div className="columns">
        <div>
          <h3>Pull requests</h3>
          {!prs ? (
            <p className="row-meta" data-testid="review-prs-loading">Loading…</p>
          ) : prs.length === 0 ? (
            <p className="row-meta" data-testid="review-prs-empty">No open PRs.</p>
          ) : (
            <ul className="session-list" data-testid="review-pr-list">
              {prs.map((p) => (
                <li key={p.number} className="detail" data-testid={`review-pr-${p.number}`}>
                  <span className="row-title">#{p.number} {p.title}</span>
                  <p className="row-meta">
                    {p.author} · {p.branch} · CI {p.ciLabel}
                    {p.isDraft ? ' · draft' : ''}
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
              {issues.map((iss) => (
                <li key={iss.number} className="detail" data-testid={`review-issue-${iss.number}`}>
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
              <div className="actions">
                <HoldToConfirm label={`Merge PR #${sel.number}`} onFire={() => void onMerge(sel.number)} />
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
              <button type="submit">Create issue</button>
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
