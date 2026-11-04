export interface RepoDiffData {
  statusShort: string;
  diffStat: string;
  diff: string;
  truncated: boolean;
}

// Read-only status + diff for one dirty checkout (issue #663).
// Rendered inline under the header while open; the full diff hides
// behind a native details toggle so long diffs don't shove the view.
export default function RepoDiffModal({
  repo,
  branch,
  data,
  error,
  onClose,
}: {
  repo: string;
  branch: string;
  data: RepoDiffData | null;
  error: string | null;
  onClose: () => void;
}) {
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
          <p className="row-meta">Changed files</p>
          <pre className="row-meta" data-testid="repo-diff-status">
            {data.statusShort || '(clean)'}
          </pre>
          {data.diffStat && (
            <>
              <p className="row-meta">Diff stat</p>
              <pre className="row-meta" data-testid="repo-diff-stat">
                {data.diffStat}
              </pre>
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
