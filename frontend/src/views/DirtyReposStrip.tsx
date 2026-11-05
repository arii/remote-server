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

export default function DirtyReposStrip({
  repos,
  onViewDiff,
  onAction,
  onAskAgent,
  onOpenChat,
}: {
  repos: RepoInfo[];
  onViewDiff: (repo: string) => void;
  onAction: (repo: string, action: RepoAction) => Promise<RepoActionResponse>;
  onAskAgent: (seed: string) => void;
  onOpenChat: (repo: string) => void;
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

  async function runAutoStashPull(repo: string) {
    const key = `${repo}:auto-stash-pull`;
    if (busy) return;
    setBusy(key);
    try {
      const stashed = await onAction(repo, 'stash');
      if (!stashed.ok) {
        setResults((prev) => ({ ...prev, [repo]: { ok: false, detail: stashed.detail } }));
        return;
      }
      const synced = await onAction(repo, 'sync');
      setResults((prev) => ({
        ...prev,
        [repo]: {
          ok: synced.ok,
          detail: synced.ok ? `${stashed.detail}; ${synced.detail}` : synced.detail,
        },
      }));
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
                    <button
                      type="button"
                      data-testid={`dirty-view-diff-${r.name}`}
                      onClick={() => onViewDiff(r.name)}
                    >
                      View Diff
                    </button>
                    <button
                      type="button"
                      data-testid={`dirty-stash-${r.name}`}
                      disabled={rowBusy}
                      onClick={() => void run(r.name, 'stash')}
                    >
                      {busy === `${r.name}:stash` ? 'Stashing…' : 'Stash'}
                    </button>
                    <button
                      type="button"
                      data-testid={`dirty-autostash-${r.name}`}
                      disabled={rowBusy}
                      onClick={() => void runAutoStashPull(r.name)}
                    >
                      {busy === `${r.name}:auto-stash-pull`
                        ? 'Working…'
                        : 'Auto-Stash & Pull'}
                    </button>
                    <button
                      type="button"
                      data-testid={`dirty-agent-${r.name}`}
                      onClick={() => onAskAgent(commitViaAgentSeed(r.name))}
                    >
                      Commit via Agent
                    </button>
                    <button
                      type="button"
                      data-testid={`dirty-pull-${r.name}`}
                      disabled={rowBusy || !r.clean}
                      title={
                        !r.clean
                          ? 'Stash or commit local changes first'
                          : undefined
                      }
                      onClick={() => void run(r.name, 'sync')}
                    >
                      {busy === `${r.name}:sync` ? 'Pulling…' : 'Pull --rebase'}
                    </button>
                    <button
                      type="button"
                      data-testid={`dirty-chat-${r.name}`}
                      onClick={() => onOpenChat(r.name)}
                    >
                      Open in Chat
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
