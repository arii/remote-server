import { useState } from 'react';
import type { RepoAction, RepoActionResponse } from '../apiClient';

export interface RepoDiffFileData {
  path: string;
  status: string;
  staged: boolean;
  unstaged: boolean;
  additions: number;
  deletions: number;
}

export interface RepoDiffData {
  statusShort: string;
  statusTruncated: boolean;
  diffStat: string;
  diff: string;
  truncated: boolean;
  files: RepoDiffFileData[];
  filesTruncated: boolean;
}

export interface FileDiff {
  path: string;
  body: string;
}

function diffHeaderPath(header: string): string {
  const rest = header.replace(/^diff --git /, '');
  const m = rest.match(/^(?:"a\/.*?"|a\/\S+)\s+(?:"b\/(.*?)"|b\/(\S+))\s*$/);
  if (m) return (m[1] ?? m[2] ?? '').trim() || rest;
  return rest;
}

// Split a unified diff blob into per-file chunks on diff --git
// headers. Pure: unit-tested, no DOM.
export function splitDiffByFile(diff: string): FileDiff[] {
  const out: FileDiff[] = [];
  let cur: string[] | null = null;
  for (const line of diff.split('\n')) {
    if (line.startsWith('diff --git ')) {
      if (cur) out.push({ path: diffHeaderPath(cur[0]), body: cur.join('\n') });
      cur = [line];
    } else if (cur) {
      cur.push(line);
    }
  }
  if (cur) out.push({ path: diffHeaderPath(cur[0]), body: cur.join('\n') });
  return out;
}

function stageNote(f: RepoDiffFileData): string {
  if (f.staged && f.unstaged) return 'staged + unstaged';
  if (f.staged) return 'staged';
  return '';
}

// Status + diff for one dirty checkout with one-click git actions
// (issues #663, #927). Rendered inline under the header while
// open; long diffs hide behind native details toggles.
export default function RepoDiffModal({
  repo,
  branch,
  data,
  error,
  onAction,
  onAskAgent,
  onClose,
}: {
  repo: string;
  branch: string;
  data: RepoDiffData | null;
  error: string | null;
  onAction: (action: RepoAction) => Promise<RepoActionResponse>;
  onAskAgent: (seed: string) => void;
  onClose: () => void;
}) {
  const [busy, setBusy] = useState<RepoAction | null>(null);
  const [result, setResult] = useState<{ ok: boolean; detail: string } | null>(null);
  const [discardArmed, setDiscardArmed] = useState(false);

  async function run(action: RepoAction) {
    if (busy) return;
    setBusy(action);
    setResult(null);
    try {
      const r = await onAction(action);
      setResult({ ok: r.ok, detail: r.detail });
    } catch (err) {
      setResult({ ok: false, detail: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(null);
    }
  }

  function tapDiscard() {
    if (discardArmed) {
      setDiscardArmed(false);
      void run('discard-untracked');
    } else {
      setDiscardArmed(true);
    }
  }

  const perFile = data ? splitDiffByFile(data.diff) : [];
  const counts = new Map(data?.files.map((f) => [f.path, f]) ?? []);
  const actionsOff = !data || !!error || busy !== null;

  return (
    <section className="detail" data-testid="repo-diff-modal" aria-label={`Diff for ${repo}`}>
      <span className="row-title">
        Diff · {repo} @ {branch}
      </span>
      {error && (
        <p role="alert" data-testid="repo-diff-error">
          Couldn&apos;t load the diff: {error}
        </p>
      )}
      {!error && !data && (
        <p className="row-meta" role="status">
          Loading diff…
        </p>
      )}
      {!error && data && (
        <>
          <p className="row-meta">
            Changed files ({data.files.length}
            {data.filesTruncated ? '+, showing first' : ''})
          </p>
          {data.files.length > 0 ? (
            <ul className="row-meta" data-testid="repo-diff-files">
              {data.files.map((f) => (
                <li key={f.path}>
                  <span>{f.status}</span> {f.path}
                  {(f.additions > 0 || f.deletions > 0) && (
                    <span>
                      {' '}
                      +{f.additions}/-{f.deletions}
                    </span>
                  )}
                  {stageNote(f) && <span> ({stageNote(f)})</span>}
                </li>
              ))}
            </ul>
          ) : (
            <pre className="row-meta" data-testid="repo-diff-status">
              (clean)
            </pre>
          )}
          {data.files.length > 0 && (
            <details data-testid="repo-diff-raw-wrap">
              <summary className="row-meta">
                Raw status{data.statusTruncated ? ' (truncated)' : ''}
              </summary>
              <pre className="row-meta" data-testid="repo-diff-status">
                {data.statusShort || '(clean)'}
              </pre>
              {data.diffStat && (
                <pre className="row-meta" data-testid="repo-diff-stat">
                  {data.diffStat}
                </pre>
              )}
            </details>
          )}
          {perFile.length > 0 && (
            <>
              <p className="row-meta">Per-file diffs</p>
              <div data-testid="repo-diff-per-file">
                {perFile.map((f) => {
                  const hit = counts.get(f.path);
                  const countNote =
                    hit && (hit.additions > 0 || hit.deletions > 0)
                      ? ` (+${hit.additions}/-${hit.deletions})`
                      : '';
                  return (
                    <details key={f.path} data-testid="repo-diff-file">
                      <summary className="row-meta">
                        {f.path}
                        {countNote}
                      </summary>
                      <pre className="row-meta">{f.body}</pre>
                    </details>
                  );
                })}
              </div>
            </>
          )}
          {data.diff && (
            <details data-testid="repo-diff-full-wrap">
              <summary className="row-meta">
                Full diff{data.truncated ? ' (truncated)' : ''}
              </summary>
              <pre className="row-meta" data-testid="repo-diff-full">
                {data.diff}
              </pre>
            </details>
          )}
          <div className="actions">
            <button
              type="button"
              data-testid="repo-diff-stash"
              disabled={actionsOff}
              onClick={() => void run('stash')}
            >
              {busy === 'stash' ? 'Stashing…' : 'Stash changes'}
            </button>
            <button
              type="button"
              data-testid="repo-diff-discard"
              disabled={actionsOff}
              onClick={tapDiscard}
            >
              {busy === 'discard-untracked'
                ? 'Discarding…'
                : discardArmed
                  ? 'Tap again to discard untracked'
                  : 'Discard untracked'}
            </button>
            <button
              type="button"
              data-testid="repo-diff-agent"
              disabled={actionsOff}
              onClick={() =>
                onAskAgent(
                  `In ${repo}, review git status and diff, commit the current changes with a clear message, and push to origin.`,
                )
              }
            >
              Commit &amp; Push via Agent
            </button>
            <button
              type="button"
              data-testid="repo-diff-sync"
              disabled={actionsOff}
              onClick={() => void run('sync')}
            >
              {busy === 'sync' ? 'Syncing…' : 'Fetch & Rebase onto origin/main'}
            </button>
          </div>
          {result && (
            <p
              role={result.ok ? 'status' : 'alert'}
              data-testid="repo-diff-action-result"
            >
              {result.detail}
            </p>
          )}
        </>
      )}
      <div className="actions">
        <button type="button" data-testid="repo-diff-close" onClick={onClose}>
          Close
        </button>
      </div>
    </section>
  );
}
