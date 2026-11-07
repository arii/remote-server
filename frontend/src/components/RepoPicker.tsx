import type { RepoInfo } from '../reviewTypes';

// Rollup pseudo-repo (issue #610): the Now attention queue across
// every checkout. Any other view must gate on this, not fetch with it.
export const ALL_REPOS = 'all';

export function isRollupRepo(repo: string): boolean {
  return repo === ALL_REPOS;
}

// Repo-only labels (#708): the trigger names the checkout without
// branch or state — coding actions carry their own branch field.
export function repoOptionLabel(r: RepoInfo): string {
  return `${r.name}${r.clean ? '' : ' (dirty)'}`;
}

function repoOptionTitle(r: RepoInfo): string {
  return `${r.full}${r.clean ? '' : ' (dirty)'}`;
}

// Per-row secondary text (#961): branch plus lag state, muted and
// right-aligned beside the row. Kept out of the label string so the
// trigger and accessible names stay branch-free (#708).
export function repoRowSecondary(r: RepoInfo): string {
  const bits: string[] = [r.branch];
  if (!r.clean) bits.push('dirty');
  if ((r.behind ?? 0) > 0) bits.push(`behind ${r.behind}`);
  if ((r.ahead ?? 0) > 0) bits.push(`ahead ${r.ahead}`);
  return bits.join(' · ');
}

// Plain native select (issue #1124): the platform dropdown replaces
// the hand-rolled button + listbox + search. Keyboard, Escape,
// focus, and type-ahead come free; nothing custom remains. Unknown
// values (a repo the console no longer lists) keep a fallback
// option so the current selection stays visible instead of
// blanking to the first row.
export default function RepoPicker({
  repos,
  value,
  onChange,
}: {
  repos: RepoInfo[];
  value: string;
  onChange: (repo: string) => void;
}) {
  const known = value === ALL_REPOS || repos.some((r) => r.name === value);
  const selected = repos.find((r) => r.name === value);
  return (
    <div className="repo-picker">
      <label className="row-meta" htmlFor="repo-picker">
        Repo{' '}
      </label>
      <select
        id="repo-picker"
        data-testid="repo-picker"
        title={
          value === ALL_REPOS
            ? 'Now attention across every checkout'
            : (selected ? repoOptionTitle(selected) : value)
        }
        value={value}
        onChange={(e) => {
          if (e.target.value !== value) onChange(e.target.value);
        }}
      >
        <option value={ALL_REPOS}>All repos</option>
        {repos.map((r) => (
          <option key={r.name} value={r.name} title={repoOptionTitle(r)}>
            {repoOptionLabel(r)}
          </option>
        ))}
        {!known && <option value={value}>{value}</option>}
      </select>
    </div>
  );
}
