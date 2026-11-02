import type { RepoInfo } from '../reviewTypes';

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
        {repos.map((r) => (
          <option key={r.name} value={r.name}>
            {r.full} · on {r.branch}
            {r.clean ? '' : ' (dirty)'}
          </option>
        ))}
      </select>
    </label>
  );
}
