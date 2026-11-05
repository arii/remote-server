import { useEffect, useRef, useState } from 'react';
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

interface PickerRow {
  name: string;
  title: string;
  secondary: string;
}

function pickerRows(repos: RepoInfo[]): PickerRow[] {
  return [
    { name: ALL_REPOS, title: 'Now attention across every checkout', secondary: '' },
    ...repos.map((r) => ({ name: r.name, title: repoOptionTitle(r), secondary: repoRowSecondary(r) })),
  ];
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
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [highlight, setHighlight] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  const selected = repos.find((r) => r.name === value);
  const trigger = value === ALL_REPOS ? 'All repos' : (selected?.name ?? value);
  const q = query.trim().toLowerCase();
  const rows = pickerRows(repos).filter((r) => r.name.toLowerCase().includes(q));

  function choose(name: string) {
    setOpen(false);
    setQuery('');
    setHighlight(0);
    if (name !== value) onChange(name);
  }

  // Close on outside tap; focus search on open.
  useEffect(() => {
    if (!open) return;
    searchRef.current?.focus();
    function onDown(e: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
        setQuery('');
      }
    }
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open ]);

  function onSearchKey(e: React.KeyboardEvent) {
    if (e.key === 'Escape') {
      e.preventDefault();
      setOpen(false);
      setQuery('');
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      setHighlight((h) => Math.min(h + 1, Math.max(rows.length - 1, 0)));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setHighlight((h) => Math.max(h - 1, 0));
    } else if (e.key === 'Enter' && rows[highlight]) {
      e.preventDefault();
      choose(rows[highlight].name);
    }
  }

  return (
    <div className="repo-picker" ref={rootRef}>
      <label className="row-meta" htmlFor="repo-picker">
        Repo{' '}
      </label>
      <button
        id="repo-picker"
        type="button"
        data-testid="repo-picker"
        title={value === ALL_REPOS ? 'Now attention across every checkout' : (selected ? repoOptionTitle(selected) : value)}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => {
          setOpen((o) => !o);
          setQuery('');
          setHighlight(0);
        }}
      >
        {trigger}
      </button>
      {open && (
        <div className="repo-menu" role="listbox" aria-label="Repositories" data-testid="repo-menu">
          <input
            ref={searchRef}
            type="search"
            data-testid="repo-search"
            placeholder="Search repos…"
            aria-label="Search repositories"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setHighlight(0);
            }}
            onKeyDown={onSearchKey}
          />
          {rows.map((r, i) => (
            <div
              key={r.name}
              role="option"
              aria-selected={r.name === value}
              data-testid={`repo-option-${r.name}`}
              data-highlighted={i === highlight || undefined}
              title={r.title}
              onClick={() => choose(r.name)}
              onMouseEnter={() => setHighlight(i)}
            >
              <span>{r.name}</span>
              {r.secondary && <span className="row-meta repo-secondary">{r.secondary}</span>}
            </div>
          ))}
          {rows.length === 0 && (
            <p className="row-meta" data-testid="repo-empty">
              No repos match.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
