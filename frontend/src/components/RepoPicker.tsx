import type { RepoInfo } from '../reviewTypes';

// Rollup pseudo-repo (issue #610): the Now attention queue across
// every checkout. Any other view must gate on this, not fetch with it.
export const ALL_REPOS = 'all';

export function isRollupRepo(repo: string): boolean {
  return repo === ALL_REPOS;
}

export function repoOptionLabel(r: RepoInfo): string {
  return `${r.name} · ${r.branch}${r.clean ? '' : ' (dirty)'}`;
}

function repoOptionTitle(r: RepoInfo): string {
  return `${r.full} · on ${r.branch}${r.clean ? '' : ' (dirty)'}`;
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
