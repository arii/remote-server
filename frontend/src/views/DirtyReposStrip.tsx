import { useState } from 'react';
import type { RepoAction, RepoActionResponse } from '../apiClient';
import type { RepoInfo } from '../reviewTypes';

// Multi-repo dirty/lag overview (#937): every dirty or behind
// checkout at a glance from Now, each row actionable without
// switching the global repo picker first. View Diff opens the
// single-repo modal for that row; the git actions carry the
// row's repo explicitly; Commit via Agent seeds Chat with the
// row's repo (same pattern as the diff modal); only Open in
// Chat navigates (it selects the row's repo and opens Chat).

export function dirtyRepos(repos: RepoInfo[]): RepoInfo[] {
  return repos.filter(
    (r) => !r.clean || (r.behind ?? 0) > 0 || (r.ahead ?? 0) > 0,
  );
}

export function repoLagLabel(r: RepoInfo): string {
  const parts: string[] = [];
  if (!r.clean) {
    const n = r.status_count ?? 0;
    parts.push(n > 0 ? `${n} changed` : 'dirty');
  }
  if ((r.behind ?? 0) > 0) parts.push(`behind ${r.behind}`);
  if ((r.ahead ?? 0) > 0) parts.push(`ahead ${r.ahead}`);
  return parts.join(' · ') || 'clean';
}

export function commitViaAgentSeed(repo: string): string {
  return `In ${repo}, review git status and diff, commit the current changes with a clear message, and push to origin.`;
}

// One primary action per row state (#958): behind + clean pulls,
// dirty hands to the agent (unblocks the pull), ahead-only drills
// into the diff (no push endpoint exists). Stash, discard, and the
// full agent handoff live in the diff modal drill-down.
export type PrimaryRepoAction = 'sync' | 'agent' | 'diff';

export function primaryRepoAction(r: RepoInfo): PrimaryRepoAction {
  if (!r.clean) return 'agent';
  if ((r.behind ?? 0) > 0) return 'sync';
  return 'diff';
}

export default function DirtyReposStrip({
  repos,
  onViewDiff,
  onAction,
  onAskAgent,
}: {
  repos: RepoInfo[];
  onViewDiff: (repo: string) => void;
  onAction: (repo: string, action: RepoAction) => Promise<RepoActionResponse>;
  onAskAgent: (seed: string) => void;
  // Retained for callers: the row no longer offers an Open-in-Chat
  // shortcut (one primary action per row, #958); the drill-down
  // modal and Chat tab cover navigation.
  onOpenChat?: (repo: string) => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, { ok: boolean; detail: string }>>({});
  const dirty = dirtyRepos(repos);
  if (dirty.length === 0) return null;

  const dirtyCount = dirty.filter((r) => !r.clean).length;
  const behindCount = dirty.filter((r) => (r.behind ?? 0) > 0).length;

  function recordFailure(repo: string, err: unknown) {
    setResults((prev) => ({
      ...prev,
      [repo]: { ok: false, detail: err instanceof Error ? err.message : String(err) },
    }));
  }

  async function run(repo: string, action: RepoAction) {
    const key = `${repo}:${action}`;
    if (busy) return;
    setBusy(key);
    try {
      const r = await onAction(repo, action);
      setResults((prev) => ({ ...prev, [repo]: { ok: r.ok, detail: r.detail } }));
    } catch (err) {
      recordFailure(repo, err);
    } finally {
      setBusy(null);
    }
  }

  return (
    <section data-testid="dirty-repos-strip" aria-label="Dirty repos">
      <h2>
        Dirty Repos ({dirty.length}){' '}
        <span className="row-meta">
          {dirtyCount} dirty · {behindCount} behind
        </span>
      </h2>
      <table data-testid="dirty-repos-table">
        <thead>
          <tr>
            <th scope="col">Repo</th>
            <th scope="col">Status</th>
            <th scope="col">Actions</th>
          </tr>
        </thead>
        <tbody>
          {dirty.map((r) => {
            const result = results[r.name];
            const rowBusy = busy !== null && busy.startsWith(`${r.name}:`);
            return (
              <tr key={r.name} data-testid={`dirty-row-${r.name}`}>
                <td>
                  <span className="row-title">{r.name}</span>{' '}
                  <span className="row-meta">
                    {r.branch ? `@ ${r.branch}` : ''}
                  </span>
                </td>
                <td className="row-meta" data-testid={`dirty-status-${r.name}`}>
                  {repoLagLabel(r)}
                </td>
                <td>
                  <div className="actions">
                    {primaryRepoAction(r) === 'sync' && (
                      <button
                        type="button"
                        data-testid={`dirty-pull-${r.name}`}
                        disabled={rowBusy}
                        onClick={() => void run(r.name, 'sync')}
                      >
                        {busy === `${r.name}:sync` ? 'Pulling…' : 'Pull --rebase'}
                      </button>
                    )}
                    {primaryRepoAction(r) === 'agent' && (
                      <button
                        type="button"
                        data-testid={`dirty-agent-${r.name}`}
                        onClick={() => onAskAgent(commitViaAgentSeed(r.name))}
                      >
                        Commit via Agent
                      </button>
                    )}
                    <button
                      type="button"
                      data-testid={`dirty-view-diff-${r.name}`}
                      onClick={() => onViewDiff(r.name)}
                    >
                      View Diff
                    </button>
                  </div>
                  {result && (
                    <p
                      role={result.ok ? 'status' : 'alert'}
                      data-testid={`dirty-result-${r.name}`}
                    >
                      {result.detail}
                    </p>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}
