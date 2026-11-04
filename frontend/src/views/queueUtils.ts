import type { DispatchItem } from '../apiTypes';

// One repo group in the queue view: every item for the repo,
// queued first, then by issue number.
export interface DispatchRepoGroup {
  repo: string;
  items: DispatchItem[];
}

export function groupQueueByRepo(items: DispatchItem[]): DispatchRepoGroup[] {
  const byRepo = new Map<string, DispatchItem[]>();
  for (const item of items) {
    const list = byRepo.get(item.repo) ?? [];
    list.push(item);
    byRepo.set(item.repo, list);
  }
  const rank = (state: string): number => {
    if (state === 'queued') return 0;
    if (state === 'running') return 1;
    if (state === 'failed') return 2;
    return 3;
  };
  return [...byRepo.entries()]
    .map(([repo, group]) => ({
      repo,
      items: [...group].sort(
        (a, b) => rank(a.state) - rank(b.state) || a.issue - b.issue,
      ),
    }))
    .sort((a, b) => (a.repo < b.repo ? -1 : a.repo > b.repo ? 1 : 0));
}

// Auto-refresh gate, mirroring jobsListActive: the queue refetches
// on an interval only while an item is still queued or running, so
// an idle Queue view makes no traffic.
export function queueListActive(items: DispatchItem[]): boolean {
  return items.some((i) => i.state === 'queued' || i.state === 'running');
}

// Next dispatchable item in a group: the first queued row. null
// when the group has nothing to dispatch (per-repo taps disable).
export function nextDispatchable(group: DispatchRepoGroup): DispatchItem | null {
  return group.items.find((i) => i.state === 'queued') ?? null;
}

// Cross-group sweep for Dispatch-all: every queued row across
// every group, in group/item order. Gates stay server-side (each
// start re-checks live; 409s report per item), so the sweep needs
// no gate snapshot to be correct.
export function allQueuedAcrossGroups(groups: DispatchRepoGroup[]): DispatchItem[] {
  return groups.flatMap((g) => g.items.filter((i) => i.state === 'queued'));
}

// Stable key for one queue row, shared by gates/ready state.
export function queueItemKey(repo: string, issue: number): string {
  return `${repo}#${issue}`;
}

// Human state line for a queue row. Terminal rows surface the
// recorded detail; live rows stay terse.
export function queueRowMeta(item: DispatchItem): string {
  if (item.state === 'queued') return 'queued';
  if (item.state === 'running') return 'running';
  if (item.detail) return `${item.state} · ${item.detail}`;
  return item.state;
}

// Dispatch tap enablement per row: queued rows dispatch, running
// rows wait, terminal rows re-dispatch (retry) or clear.
export function queueRowActions(state: string): { dispatch: boolean; cancel: boolean } {
  if (state === 'queued') return { dispatch: true, cancel: true };
  if (state === 'running') return { dispatch: false, cancel: false };
  return { dispatch: true, cancel: true };
}

// Blocked-dispatch notice from a 409 reason: the server joins gate
// reasons with '; ', so split them back into lines.
export function splitBlockReasons(detail: string): string[] {
  return detail
    .split(';')
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}
