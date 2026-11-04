import type { RepoInfo } from '../reviewTypes';

// Rollup pseudo-repo (issue #610): the Now attention queue across
// every checkout. Any other view must gate on this, not fetch with it.
export const ALL_REPOS = 'all';

export function isRollupRepo(repo: string): boolean {
  return repo === ALL_REPOS;
}

// Repo-only labels (#708): the header is repo-scoped, and the
// checkout branch never scopes a repo-generic view, so it is not
// shown here. Coding actions carry their own branch field.
export function repoOptionLabel(r: RepoInfo): string {
  return `${r.name}${r.clean ? '' : ' (dirty)'}`;
}

function repoOptionTitle(r: RepoInfo): string {
  return `${r.full}${r.clean ? '' : ' (dirty)'}`;
}

export default function RepoPicker({
  repos,
  value,
  onChange,
}: {
  repos: RepoInfo[];
  value: string;
  onChange: (repo: string) => void;
}) {
  return (
    <label className="row-meta" htmlFor="repo-picker">
      Repo{' '}
      <select
        id="repo-picker"
        data-testid="repo-picker"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value={ALL_REPOS} title="Now attention across every checkout">
          All repos
        </option>
        {repos.map((r) => (
          <option key={r.name} value={r.name} title={repoOptionTitle(r)}>
            {repoOptionLabel(r)}
          </option>
        ))}
      </select>
    </label>
  );
}
