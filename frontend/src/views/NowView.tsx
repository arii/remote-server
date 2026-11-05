import { Fragment, useEffect, useRef, useState } from 'react';
import TapToFire from '../components/TapToFire';
import { isRollupRepo } from '../components/RepoPicker';
import type { RepoInfo } from '../reviewTypes';
import {
  apiUrl,
  createIssue,
  fetchDeferred,
  fetchInbox,
  fetchJobs,
  fetchPrs,
  fetchRepoLabels,
  postActionHref,
  snoozeAttention,
  unsnoozeAttention,
  warmViewCaches,
} from '../apiClient';
import type { InboxFeed, SystemHealthResponse } from '../apiClient';
import type { JobRecord } from '../apiTypes';
import type { AttentionItem } from '../attentionTypes';
import type { DeferredRow } from '../reviewTypes';
import { loadInbox, loadInboxEmpty } from '../mocks/inbox';
import { ContainerPills } from './ObservabilitySection';
import InboxStrips from './InboxStrips';
import ReviewView from './ReviewView';
import JobsView from './JobsView';
import QueueView from './QueueView';
import { loadJobs, loadPrs, useMock } from './mockViews';

function isGetAction(item: AttentionItem): boolean {
  return item.action.method.toUpperCase() === 'GET';
}

async function firePostAction(item: AttentionItem): Promise<void> {
  // Central outbox: offline taps queue + throw visibly instead of
  // failing with no recourse (issue #404).
  await postActionHref(item.action.href, { intent: item.action.intent, confirmed: true });
}

// GET action navigation (issue #464): backend hrefs point at JSON
// endpoints whose payloads already have in-app surfaces. Navigate
// there instead of fetching-and-discarding. Unknown hrefs keep the
// fetch-summary fallback. Actions must never render as plain links:
// that navigates the SPA away to raw JSON.
export type AttentionNavTarget = 'jobs' | 'review' | 'history' | 'briefing' | 'chat';

export function resolveAttentionTarget(href: string): AttentionNavTarget | null {
  const path = href.split('?')[0].split('#')[0].replace(/^\/+/, '');
  if (path === 'api/jobs' || path.startsWith('api/jobs/')) return 'jobs';
  if (path === 'api/prs' || path.startsWith('api/prs/')) return 'review';
  if (path === 'api/workflow-health') return 'history';
  if (path === 'api/notifications/status') return 'briefing';
  if (path === 'api/agents/budget') return 'chat';
  return null;
}

export type AttentionNavTab = 'history' | 'briefing' | 'chat';

async function fireGetAction(item: AttentionItem): Promise<string> {
  const resp = await fetch(apiUrl(item.action.href), { method: 'GET' });
  if (!resp.ok) throw new Error(`Action failed: ${resp.status}`);
  let summary = '';
  try {
    const body: unknown = await resp.json();
    if (body && typeof body === 'object') {
      const o = body as Record<string, unknown>;
      const cand = o.message ?? o.detail ?? o.status ?? o.url;
      if (typeof cand === 'string' && cand) summary = cand;
    }
  } catch {
    summary = '';
  }
  return summary ? `${item.action.label}: ${summary}` : `${item.action.label}: done.`;
}

function formatStamp(epochSeconds: number): string {
  if (!epochSeconds) return 'unknown time';
  return new Date(epochSeconds * 1000).toLocaleString();
}

function relativeStamp(epochSeconds: number): string {
  if (!epochSeconds) return 'unknown time';
  const mins = Math.max(0, Math.round((Date.now() / 1000 - epochSeconds) / 60));
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

// Live queue cadence: re-fetch fast while items need the operator,
// back off to a slow heartbeat when the queue is clear.
export function nowPollMs(itemCount: number): number {
  return itemCount > 0 ? 15000 : 60000;
}

// Attention snooze presets (issue #614), minutes. Server validates.
export const SNOOZE_PRESETS = [60, 480];

export function snoozePresetLabel(minutes: number): string {
  return minutes >= 60 ? `Snooze ${minutes / 60}h` : `Snooze ${minutes}m`;
}

export interface SnoozedEntry {
  until: number;
  title: string;
}

// Drop stubs that no longer apply: expired ones (the item resurfaces
// on its own) and stubs whose item is back in the list (server truth
// wins over optimism).
export function reconcileSnoozed(
  prev: Record<string, SnoozedEntry>,
  liveIds: string[],
  nowSec: number,
): Record<string, SnoozedEntry> {
  const next: Record<string, SnoozedEntry> = {};
  for (const [id, entry] of Object.entries(prev)) {
    if (entry.until > nowSec && !liveIds.includes(id)) next[id] = entry;
  }
  return next;
}

// Stubs for snoozed ids missing from the live list (the undo row);
// present ids render their stub inline instead.
export function snoozedRows(
  snoozed: Record<string, SnoozedEntry>,
  liveIds: string[],
): Array<[string, SnoozedEntry]> {
  return Object.entries(snoozed).filter(([id]) => !liveIds.includes(id));
}

export function SnoozeButtons({
  itemId,
  busy,
  onSnooze,
}: {
  itemId: string;
  busy: boolean;
  onSnooze: (minutes: number) => void;
}) {
  return (
    <>
      {SNOOZE_PRESETS.map((m) => (
        <button
          key={m}
          type="button"
          data-testid={`snooze-${itemId}-${m}`}
          disabled={busy}
          onClick={() => onSnooze(m)}
        >
          {snoozePresetLabel(m)}
        </button>
      ))}
    </>
  );
}

export function SnoozedStub({
  itemId,
  title,
  until,
  busy,
  onUnsnooze,
}: {
  itemId: string;
  title: string;
  until: number;
  busy: boolean;
  onUnsnooze: () => void;
}) {
  return (
    <li className="detail" data-testid={`now-snoozed-${itemId}`}>
      <span className="row-title">Snoozed: {title}</span>
      <p className="row-meta">Hidden until {formatStamp(until)}</p>
      <div className="actions">
        <button
          type="button"
          data-testid={`unsnooze-${itemId}`}
          disabled={busy}
          onClick={onUnsnooze}
        >
          {busy ? 'Unsnoozing…' : 'Unsnooze'}
        </button>
      </div>
    </li>
  );
}

export function StackSection({
  health,
  onOpenStats,
}: {
  health: SystemHealthResponse | null;
  onOpenStats?: (service?: string) => void;
}) {
  const allUp =
    health !== null &&
    health.containers.length > 0 &&
    health.containers.every(
      (c) =>
        (c.state ?? '').toLowerCase() === 'running' &&
        (c.health ?? '').toLowerCase() !== 'unhealthy',
    );
  return (
    <section data-testid="now-stack" aria-label="Stack">
      <h2>Stack</h2>
      {!health ? (
        <p className="row-meta" role="status">Stack status: loading…</p>
      ) : allUp ? (
        <p className="row-meta" data-testid="now-stack-clear" role="status">
          Stack: all {health.containers.length} containers up.
        </p>
      ) : (
        <ContainerPills containers={health.containers} error={health.containers_error} onOpenStats={onOpenStats} />
      )}
    </section>
  );
}

// Repo tag (issue #610, extended #723): rollup rows always tag;
// single-repo rows tag only when the row's repo differs from the
// view — foreign rows should not occur after the backend scoping
// fix, so single-repo mode stays pixel-identical and the tag is
// the backstop that keeps a leak distinguishable.
// Guided empty state (issue #809): what lands here plus the
// one-tap first action (opens the Jobs section to run a check).
export function NowEmptyState({ onRunCheck }: { onRunCheck: () => void }) {
  return (
    <div data-testid="now-empty">
      <h3>Nothing needs you right now</h3>
      <p className="row-meta">Failing checks, approvals, and incidents will land here.</p>
      <div className="actions">
        <button type="button" data-testid="now-empty-jobs" onClick={onRunCheck}>
          Run a check
        </button>
      </div>
    </div>
  );
}

export function RepoTag({ repo }: { repo: string }) {
  return (
    <span className="repo-chip" data-testid={`now-repo-${repo}`}>
      {repo}
    </span>
  );
}

export function showRepoTag(rollup: boolean, itemRepo: string, viewRepo: string): boolean {
  return rollup || itemRepo !== viewRepo;
}

// Ask-agent seed (issue #723): always the row's own repo, never
// the view repo — a foreign row must still seed its own context.
export function askAgentSeed(item: AttentionItem): string {
  return `${item.title} (${item.repo}): ${item.detail}`;
}

function sevClass(severity: string): string {
  const s = severity.toLowerCase();
  if (s === 'crit' || s === 'critical') return 'sev sev-crit';
  if (s === 'warn' || s === 'warning') return 'sev sev-warn';
  return 'sev sev-info';
}

export function ReportDialog({
  initialBody,
  availableLabels = [],
  onSubmit,
  onCancel,
}: {
  initialBody: string;
  availableLabels?: string[];
  onSubmit: (title: string, body: string, labels: string[]) => void;
  onCancel: () => void;
}) {
  const [title, setTitle] = useState('');
  const [body, setBody] = useState(initialBody);
  const [labels, setLabels] = useState<string[]>([]);
  function toggleLabel(name: string) {
    setLabels((prev) => (prev.includes(name) ? prev.filter((l) => l !== name) : [...prev, name]));
  }
  // Inline (non-modal) dialog: labelled, Escape-dismissible, focus lands in
  // the title field on open. No focus trap: the page behind stays usable.
  return (
    <form
      data-testid="report-dialog"
      role="dialog"
      aria-label="Report a bug"
      onKeyDown={(e) => {
        if (e.key === 'Escape') {
          e.preventDefault();
          onCancel();
        }
      }}
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit(title.trim(), body, labels);
      }}
    >
      {availableLabels.length > 0 && (
        <fieldset data-testid="report-labels">
          <legend className="row-meta">Labels (optional)</legend>
          {availableLabels.map((name) => (
            <label key={name} className="row-meta">
              <input
                type="checkbox"
                data-testid={`report-label-${name}`}
                checked={labels.includes(name)}
                onChange={() => toggleLabel(name)}
              />{' '}
              {name}
            </label>
          ))}
        </fieldset>
      )}
      <label className="row-meta" htmlFor="report-title">
        Bug title (required){' '}
        <input
          id="report-title"
          data-testid="report-title"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          required
          aria-required="true"
          autoFocus
        />
      </label>
      <label className="row-meta" htmlFor="report-body">
        Details (optional){' '}
        <textarea
          id="report-body"
          data-testid="report-body"
          value={body}
          onChange={(e) => setBody(e.target.value)}
        />
      </label>
      <div className="actions">
        <button type="submit" data-testid="report-submit">
          File bug
        </button>
        <button type="button" data-testid="report-cancel" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

export function ActionControl({
  item,
  onDone,
  onError,
  onSection,
  onTab,
}: {
  item: AttentionItem;
  onDone: (title: string) => void;
  onError: (message: string) => void;
  onSection: (section: 'review' | 'jobs') => void;
  onTab: (tab: AttentionNavTab) => void;
}) {
  async function handlePost() {
    try {
      await firePostAction(item);
      onDone(item.title);
    } catch (err) {
      onError(err instanceof Error ? err.message : String(err));
    }
  }
  async function handleGet() {
    const target = resolveAttentionTarget(item.action.href);
    if (target === 'jobs') {
      onSection('jobs');
      return;
    }
    if (target === 'review') {
      onSection('review');
      return;
    }
    if (target !== null) {
      onTab(target);
      return;
    }
    try {
      onDone(await fireGetAction(item));
    } catch (err) {
      onError(err instanceof Error ? err.message : String(err));
    }
  }
  if (isGetAction(item)) {
    return (
      <button type="button" className="btn-primary" onClick={() => void handleGet()}>
        {item.action.label}
      </button>
    );
  }
  if (item.action.destructive) {
    return (
      <TapToFire
        label={item.action.label}
        confirm="This cannot be undone."
        testid={`now-action-${item.id}`}
        onFire={() => void handlePost()}
      />
    );
  }
  return (
    <button type="button" className="btn-primary" onClick={() => void handlePost()}>
      {item.action.label}
    </button>
  );
}

// Status hero (issue #811): one glanceable strip atop Now answering
// "is everything fine?". Every count comes from a live fetch —
// inbox, PRs, jobs, system health — never invented. Worst status
// wins: a sick container or crit item reads crit, any queued item
// or a silent stack reads warn, otherwise all clear. All clear is
// reserved for a truly empty queue (issue #930): even an info-only
// item keeps the banner amber, so the dot never contradicts the
// "N needing attention" count beside it.
export type HeroStatus = 'loading' | 'ok' | 'warn' | 'crit';

export function heroStatus(
  health: SystemHealthResponse | null,
  attention: AttentionItem[],
): HeroStatus {
  if (!health) return 'loading';
  if (health.containers_error) return 'crit';
  const stackBad = health.containers.some(
    (c) =>
      (c.state ?? '').toLowerCase() !== 'running'
      || (c.health ?? '').toLowerCase() === 'unhealthy',
  );
  if (stackBad) return 'crit';
  const sevs = new Set(attention.map((i) => i.severity.toLowerCase()));
  if (sevs.has('crit') || sevs.has('critical')) return 'crit';
  if (attention.length > 0) return 'warn';
  if (health.containers.length === 0) return 'warn';
  return 'ok';
}

const HERO_SEV_RANK: Record<string, number> = {
  crit: 0,
  critical: 0,
  warn: 1,
  warning: 1,
  info: 2,
};

// Most urgent attention item: worst severity wins, backend feed
// order breaks ties (stable — the first of the worst ranks).
export function topUrgent(items: AttentionItem[]): AttentionItem | null {
  if (items.length === 0) return null;
  let best = items[0];
  for (const item of items) {
    const rank = HERO_SEV_RANK[item.severity.toLowerCase()] ?? 2;
    const bestRank = HERO_SEV_RANK[best.severity.toLowerCase()] ?? 2;
    if (rank < bestRank) best = item;
  }
  return best;
}

// Repeating-failure grouping (issue #839): one accordion row per
// kind + title instead of one card per repeat. Members keep feed
// order; the group severity is the worst member rank.
export interface AttentionGroup {
  key: string;
  kind: string;
  title: string;
  severity: string;
  items: AttentionItem[];
  firstSeen: number;
  lastSeen: number;
}

export function attentionGroupKey(item: AttentionItem): string {
  return `${item.kind}‖${item.title}`;
}

function groupWorstSeverity(items: AttentionItem[]): string {
  let worst = items[0].severity;
  let worstRank = HERO_SEV_RANK[worst.toLowerCase()] ?? 2;
  for (const item of items) {
    const rank = HERO_SEV_RANK[item.severity.toLowerCase()] ?? 2;
    if (rank < worstRank) {
      worst = item.severity;
      worstRank = rank;
    }
  }
  return worst;
}

export function groupAttentionItems(items: AttentionItem[]): AttentionGroup[] {
  const groups = new Map<string, AttentionItem[]>();
  for (const item of items) {
    const key = attentionGroupKey(item);
    const list = groups.get(key);
    if (list) list.push(item);
    else groups.set(key, [item]);
  }
  return [...groups.entries()].map(([key, members]) => {
    const stamps = members.map((m) => m.created_at).filter((t) => t > 0);
    return {
      key,
      kind: members[0].kind,
      title: members[0].title,
      severity: groupWorstSeverity(members),
      items: members,
      firstSeen: stamps.length > 0 ? Math.min(...stamps) : 0,
      lastSeen: stamps.length > 0 ? Math.max(...stamps) : 0,
    };
  });
}

export function groupSeenText(group: AttentionGroup): string {
  if (!group.firstSeen) return 'unknown time';
  if (group.items.length < 2 || group.firstSeen === group.lastSeen) {
    return relativeStamp(group.firstSeen);
  }
  return `first ${relativeStamp(group.firstSeen)} · last ${relativeStamp(group.lastSeen)}`;
}

// Console self-deploy action (engine allowlist): the hero's last
// deploy age is the newest deploy-buttons run's start.
export const DEPLOY_ACTION = 'deploy-buttons';

export function lastDeployAt(jobs: JobRecord[]): number | null {
  let best: number | null = null;
  for (const job of jobs) {
    if ((job.kind ?? job.name ?? '') !== DEPLOY_ACTION) continue;
    const at = job.at ?? job.created_at ?? null;
    if (typeof at !== 'number' || !at) continue;
    if (best === null || at > best) best = at;
  }
  return best;
}

export interface HeroCounts {
  attentionOpen: number;
  runsActive: boolean;
  // Null in the rollup: open PRs are a per-repo count.
  prCount: number | null;
  // Null when no deploy run is on record.
  deployAt: number | null;
}

// Banner deep links (issue #930): every count segment navigates
// somewhere useful — the queue count to the inbox home, PRs to
// the Review section, runs to Jobs, deploys to History.
export type HeroSegmentTarget = 'attention' | 'review' | 'jobs' | 'history';

export interface HeroSegment {
  key: 'attention' | 'runs' | 'prs' | 'deploys';
  text: string;
  target: HeroSegmentTarget;
}

export function heroSegments({
  attentionOpen,
  runsActive,
  prCount,
  deployAt,
}: HeroCounts): HeroSegment[] {
  const segments: HeroSegment[] = [
    { key: 'attention', text: `${attentionOpen} needing attention`, target: 'attention' },
    { key: 'runs', text: runsActive ? '1 active run' : 'no active runs', target: 'jobs' },
  ];
  if (prCount !== null) {
    segments.push({
      key: 'prs',
      text: `${prCount} PR${prCount === 1 ? '' : 's'} open`,
      target: 'review',
    });
  }
  segments.push({
    key: 'deploys',
    text: deployAt ? `deploy ${relativeStamp(deployAt)}` : 'no recent deploys',
    target: 'history',
  });
  return segments;
}

export function heroCountsText(counts: HeroCounts): string {
  return heroSegments(counts).map((s) => s.text).join(' · ');
}

export function fireHeroSegment(
  target: HeroSegmentTarget,
  cbs: {
    onAttention: () => void;
    onSection: (section: 'review' | 'jobs') => void;
    onHistoryDeploys: () => void;
  },
): void {
  if (target === 'attention') cbs.onAttention();
  else if (target === 'history') cbs.onHistoryDeploys();
  else cbs.onSection(target);
}

const HERO_DOT: Record<HeroStatus, string> = {
  loading: 'dot dot-idle',
  ok: 'dot dot-ok',
  warn: 'dot dot-warn',
  crit: 'dot dot-bad',
};

// Dynamic status label (issue #930): a non-empty queue always
// reads its depth ("N needing attention") with the dot carrying
// the severity; an empty queue names the stack state instead.
export function heroLabel(status: HeroStatus, attentionOpen: number): string {
  if (status === 'loading') return 'Loading status…';
  if (status === 'ok') return 'All clear';
  if (attentionOpen > 0) return `${attentionOpen} needing attention`;
  return status === 'crit' ? 'Action needed' : 'Needs attention';
}

export function NowHero({
  health,
  feed,
  prCount,
  deployAt,
  onDone,
  onError,
  onSection,
  onTab,
  onAttention,
  onHistoryDeploys,
}: {
  health: SystemHealthResponse | null;
  feed: InboxFeed;
  prCount: number | null;
  deployAt: number | null;
  onDone: (title: string) => void;
  onError: (message: string) => void;
  onSection: (section: 'review' | 'jobs') => void;
  onTab: (tab: AttentionNavTab) => void;
  onAttention: () => void;
  onHistoryDeploys: () => void;
}) {
  const status = heroStatus(health, feed.attention);
  const top = topUrgent(feed.attention);
  const segments = heroSegments({
    attentionOpen: feed.attention.length,
    runsActive: feed.runs.active !== null,
    prCount,
    deployAt,
  });
  return (
    <section data-testid="now-hero" aria-label="Status summary" className="health now-hero">
      <span aria-hidden="true" data-testid="now-hero-dot" className={HERO_DOT[status]} />
      <strong data-testid="now-hero-status">{heroLabel(status, feed.attention.length)}</strong>
      <span className="row-meta" data-testid="now-hero-counts">
        {segments.map((seg, i) => (
          <Fragment key={seg.key}>
            {i > 0 && <span aria-hidden="true"> · </span>}
            <button
              type="button"
              className="btn-link"
              data-testid={`now-hero-seg-${seg.key}`}
              onClick={() => fireHeroSegment(seg.target, { onAttention, onSection, onHistoryDeploys })}
            >
              {seg.text}
            </button>
          </Fragment>
        ))}
      </span>
      <span className="spacer" aria-hidden="true" />
      {top ? (
        <span data-testid="now-hero-urgent">
          <span className="row-meta">Top: {top.title} </span>
          <ActionControl
            item={top}
            onDone={onDone}
            onError={onError}
            onSection={onSection}
            onTab={onTab}
          />
        </span>
      ) : (
        <span className="row-meta" data-testid="now-hero-urgent">
          Nothing urgent.
        </span>
      )}
    </section>
  );
}

export function attentionTargetName(href: string): string {
  const target = resolveAttentionTarget(href);
  if (!target) return 'action';
  return target.charAt(0).toUpperCase() + target.slice(1);
}

export interface AttentionRowCallbacks {
  onDone: (title: string) => void;
  onError: (message: string) => void;
  onSection: (section: 'review' | 'jobs') => void;
  onTab: (tab: AttentionNavTab) => void;
  onAskAgent: (seed: string) => void;
  onSnooze: (item: AttentionItem, minutes: number) => void;
}

// Source-line destination (#896): when the action resolves to an
// in-app target, the "opens X" run navigates there (same dispatch
// as ActionControl). Fetch-only actions render nothing (issue
// #930): the old "opens action" read as routing debug text, and
// the action button below the line already says what it does.
export function SourceDestination({
  item,
  onSection,
  onTab,
}: {
  item: AttentionItem;
  onSection: (section: 'review' | 'jobs') => void;
  onTab: (tab: AttentionNavTab) => void;
}) {
  const target = resolveAttentionTarget(item.action.href);
  const name = attentionTargetName(item.action.href);
  if (target === null) return null;
  return (
    <button
      type="button"
      className="btn-link"
      data-testid={`now-source-open-${item.id}`}
      onClick={() => {
        if (target === 'jobs' || target === 'review') onSection(target);
        else onTab(target);
      }}
    >
      {name}
    </button>
  );
}

// Concrete repo context for the source line (issue #930): the
// row's repo plus its checkout branch and a dirty-worktree flag
// when known. Unknown repos degrade to the bare name; clean
// checkouts stay quiet (clean is the default, not news).
export function repoContextText(repo: string, info: RepoInfo | null | undefined): string {
  const branch = info?.branch ? ` (${info.branch})` : '';
  const dirty = info && !info.clean ? ' · dirty worktree' : '';
  return `repo: ${repo}${branch}${dirty}`;
}

// One attention row (issue #839): the primary action + Ask agent
// stay visible; snooze presets collapse into a More overflow so a
// queued item costs one line of buttons, not four. The source line
// names the row's repo context and, when the action resolves to
// an in-app surface, its destination.
export function AttentionRow({
  item,
  viewRepo,
  rollup,
  repoInfo,
  selected,
  busy,
  onToggleSelect,
  onDone,
  onError,
  onSection,
  onTab,
  onAskAgent,
  onSnooze,
}: AttentionRowCallbacks & {
  item: AttentionItem;
  viewRepo: string;
  rollup: boolean;
  repoInfo?: RepoInfo | null;
  selected: boolean;
  busy: boolean;
  onToggleSelect: () => void;
}) {
  return (
    <li className="detail" data-testid={`now-item-${item.id}`}>
      <input
        type="checkbox"
        data-testid={`now-select-${item.id}`}
        aria-label={`Select ${item.title}`}
        checked={selected}
        onChange={onToggleSelect}
      />{' '}
      {showRepoTag(rollup, item.repo, viewRepo) ? (
        <span className="row-title">
          <RepoTag repo={item.repo} /> {item.title}
        </span>
      ) : (
        <span className="row-title">{item.title}</span>
      )}
      <p className="row-meta">
        {item.kind} · <span className={sevClass(item.severity)}>{item.severity}</span>
        {' · '}
        <span title={formatStamp(item.created_at)}>
          {relativeStamp(item.created_at)}
        </span>
      </p>
      <p className="row-meta" data-testid={`now-source-${item.id}`}>
        {repoContextText(item.repo, repoInfo)}
        {resolveAttentionTarget(item.action.href) !== null && (
          <>
            {' · opens '}
            <SourceDestination item={item} onSection={onSection} onTab={onTab} />
          </>
        )}
      </p>
      <p>{item.detail}</p>
      <div className="actions">
        <ActionControl
          item={item}
          onDone={onDone}
          onError={onError}
          onSection={onSection}
          onTab={onTab}
        />
        <button
          type="button"
          className="btn-secondary"
          data-testid={`ask-agent-${item.id}`}
          onClick={() => onAskAgent(askAgentSeed(item))}
        >
          Ask agent
        </button>
        <details className="row-overflow" data-testid={`now-more-${item.id}`}>
          <summary>More</summary>
          <div className="actions">
            <SnoozeButtons
              itemId={item.id}
              busy={busy}
              onSnooze={(m) => onSnooze(item, m)}
            />
          </div>
        </details>
      </div>
    </li>
  );
}

// One accordion row for a repeating failure: count, worst
// severity, and first/last seen in the summary; members expand
// below with their own actions.
export function AttentionGroupRow({
  group,
  index,
  children,
}: {
  group: AttentionGroup;
  index: number;
  children: React.ReactNode;
}) {
  return (
    <li className="detail attention-group" data-testid={`now-group-${index}`}>
      <details>
        <summary>
          <span className="row-title">
            {group.title} ×{group.items.length}
          </span>
          <span className="row-meta">
            {group.kind} · <span className={sevClass(group.severity)}>{group.severity}</span>
            {' · '}
            {groupSeenText(group)}
          </span>
        </summary>
        <ul className="session-list">{children}</ul>
      </details>
    </li>
  );
}

// Bulk bar: rendered only while attention rows are selected. One
// snooze action per preset across the selection, plus clear.
export function AttentionBulkBar({
  count,
  busy,
  onSnooze,
  onClear,
}: {
  count: number;
  busy: boolean;
  onSnooze: (minutes: number) => void;
  onClear: () => void;
}) {
  return (
    <div className="actions" data-testid="now-bulk-bar" role="status">
      {count} selected{' '}
      {SNOOZE_PRESETS.map((m) => (
        <button
          key={m}
          type="button"
          data-testid={`now-bulk-snooze-${m}`}
          aria-label={`${snoozePresetLabel(m)} ${count} selected item${count === 1 ? '' : 's'}`}
          disabled={busy}
          onClick={() => onSnooze(m)}
        >
          {snoozePresetLabel(m)} ({count})
        </button>
      ))}
      <button type="button" data-testid="now-bulk-clear" onClick={onClear}>
        Clear
      </button>
    </div>
  );
}

export default function NowView({
  repo,
  defaultBranch,
  repos = [],
  health,
  reportContext,
  onAskAgent,
  onOpenTab,
  onOpenHistoryQuery,
  sectionRequest = null,
  reportRequest = 0,
  createRequest = 0,
  onSectionChange,
  onOpenStats,
}: {
  repo: string;
  // Checkout branch of the viewed repo, seeding the Review/Jobs
  // branch fields (#708).
  defaultBranch?: string;
  // Known checkouts, backing the attention rows' repo context
  // (issue #930). Rows look up their own repo by name, so rollup
  // rows resolve as well as single-repo ones.
  repos?: RepoInfo[];
  health: SystemHealthResponse | null;
  reportContext: string;
  onAskAgent: (seed: string) => void;
  onOpenTab: (tab: AttentionNavTab) => void;
  // History deep link (issue #930): opens History with its search
  // seeded. Absent (older callers, tests), the deploy segment
  // falls back to a plain History tab switch.
  onOpenHistoryQuery?: (query: string) => void;
  // Command-palette requests (issue #810): nonce bumps from App that
  // open a section or the report dialog, including on a fresh mount
  // after the palette navigates here from another tab. The nav bar
  // (#896, Queue joins in #930) reuses the same path; a null
  // section closes whichever is open (Now = inbox home).
  sectionRequest?: { section: 'review' | 'jobs' | 'queue' | null; nonce: number } | null;
  reportRequest?: number;
  // Palette request (issue #839): a nonce bump opens the Review
  // section's New-issue form (threaded through to ReviewView).
  createRequest?: number;
  // Section mirror for the nav active state (#896): notified on
  // every section change (including mount and repo-switch resets),
  // so App never shows a stale entry pressed.
  onSectionChange?: (section: 'review' | 'jobs' | 'queue' | null) => void;
  // Opens the container stats drawer (#896): Stack pills navigate.
  onOpenStats?: (service?: string) => void;
}) {
  const [data, setData] = useState<InboxFeed | null>(null);
  // Deferred flags backing the strips bulk bar (issue #591). The
  // strips render outside the rollup only; rollup keeps [].
  const [deferred, setDeferred] = useState<DeferredRow[]>([]);
  // Hero extras (issue #811): open-PR count (per-repo; the rollup
  // keeps null and hides the segment) and last deploy start.
  const [prCount, setPrCount] = useState<number | null>(null);
  const [deployAt, setDeployAt] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fired, setFired] = useState<string | null>(null);
  const [reported, setReported] = useState<string | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  // Repo labels for the report dialog checkboxes (issue #436):
  // fetched once when the dialog first opens, like ReviewView's
  // typeahead suggestions; null until loaded.
  const [reportLabels, setReportLabels] = useState<string[] | null>(null);
  // One section mounted at a time: Review and Jobs each bind window
  // triage keys, so mounting both would double-bind j/k/Enter.
  // Queue binds none, but still mounts solo for a stable layout.
  const [section, setSection] = useState<'review' | 'jobs' | 'queue' | null>(null);
  // Last Review row selection, surviving section switches so Jobs can
  // prefill its PR field from row context. Reset on repo change.
  const [lastPr, setLastPr] = useState<number | null>(null);
  // Optimistic snooze stubs by item id. A stub collapses its card
  // instantly; refresh() reconciles against the server list.
  const [snoozed, setSnoozed] = useState<Record<string, SnoozedEntry>>({});
  const [snoozeBusy, setSnoozeBusy] = useState<string | null>(null);
  // Bulk selection across attention rows (issue #839): snooze-n via
  // the bulk bar instead of tapping each row's overflow.
  const [bulkSelected, setBulkSelected] = useState<ReadonlySet<string>>(new Set());
  const rollup = isRollupRepo(repo);

  useEffect(() => {
    setLastPr(null);
    // Review/Jobs are per-repo sections; a repo switch (in or out
    // of the rollup) closes whichever is open.
    setSection(null);
  }, [repo]);

  // Palette requests: each nonce handled once; stale nonces from a
  // previous mount still fire, since the palette navigated here.
  const handledSectionNonce = useRef(0);
  useEffect(() => {
    if (sectionRequest && sectionRequest.nonce !== handledSectionNonce.current) {
      handledSectionNonce.current = sectionRequest.nonce;
      setSection(sectionRequest.section);
    }
  }, [sectionRequest]);
  // Mirror the open section up for the nav active state (#896). One
  // effect covers every path (internal toggles, palette/nav nonces,
  // repo-switch resets, mount) with no call-site edits.
  useEffect(() => {
    onSectionChange?.(section);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [section]);
  const handledReportNonce = useRef(0);
  useEffect(() => {
    if (reportRequest !== handledReportNonce.current) {
      handledReportNonce.current = reportRequest;
      if (reportRequest > 0) {
        setDialogOpen(true);
        void ensureReportLabels();
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reportRequest]);

  // Hero extras ride the inbox refresh so the strip never goes
  // stale. Best effort: a failed extra keeps its last value.
  async function refreshHeroExtras() {
    if (useMock()) {
      setPrCount(rollup ? null : loadPrs().length);
      setDeployAt(lastDeployAt(loadJobs().jobs));
      return;
    }
    if (!rollup) {
      try {
        setPrCount((await fetchPrs(repo)).prs.length);
      } catch {
        // Keeps the last count; Review shows its own error.
      }
    } else {
      setPrCount(null);
    }
    try {
      setDeployAt(lastDeployAt((await fetchJobs()).jobs));
    } catch {
      // Keeps the last deploy age.
    }
  }

  async function refresh() {
    try {
      const live = await fetchInbox(repo);
      setData(live);
      if (!rollup) {
        try {
          const d = await fetchDeferred(repo);
          setDeferred(d.deferred);
        } catch {
          // Strips keep their last flags; Review shows its own error.
        }
      }
      await refreshHeroExtras();
      setSnoozed((prev) =>
        reconcileSnoozed(
          prev,
          live.attention.map((i) => i.id),
          Date.now() / 1000,
        ),
      );
      // A bulk-selected row that left the list drops from the
      // selection so the count never names a resolved item.
      setBulkSelected((prev) => {
        const liveIds = new Set(live.attention.map((i) => i.id));
        if ([...prev].every((id) => liveIds.has(id))) return prev;
        return new Set([...prev].filter((id) => liveIds.has(id)));
      });
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  function dropStub(id: string) {
    setSnoozed((prev) => {
      if (!(id in prev)) return prev;
      const next = { ...prev };
      delete next[id];
      return next;
    });
  }

  async function onSnooze(item: AttentionItem, minutes: number) {
    if (snoozeBusy !== null) return;
    const optimisticUntil = Date.now() / 1000 + minutes * 60;
    setSnoozed((prev) => ({ ...prev, [item.id]: { until: optimisticUntil, title: item.title } }));
    setSnoozeBusy(item.id);
    try {
      // Snooze against the row's own repo: the view repo is the
      // rollup pseudo-repo in All repos mode, where rows carry
      // their own repos.
      const res = await snoozeAttention(item.id, minutes, item.repo);
      // Re-assert with the server timestamp: a poll refresh may have
      // dropped the optimistic stub mid-flight.
      setSnoozed((prev) => ({ ...prev, [item.id]: { until: res.snoozed_until, title: item.title } }));
      setFired(`Snoozed: ${item.title} until ${formatStamp(res.snoozed_until)}`);
      await refresh();
    } catch (err) {
      dropStub(item.id);
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSnoozeBusy(null);
    }
  }

  async function onUnsnooze(id: string) {
    if (snoozeBusy !== null) return;
    setSnoozeBusy(id);
    try {
      await unsnoozeAttention(id);
      dropStub(id);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSnoozeBusy(null);
    }
  }

  // One preset across the whole bulk selection: snooze each live
  // row, then refresh once. A mid-loop failure keeps the stubs that
  // landed and reports; refresh reconciles the rest.
  async function onBulkSnooze(minutes: number) {
    if (snoozeBusy !== null || !data) return;
    const targets = data.attention.filter((i) => bulkSelected.has(i.id));
    if (targets.length === 0) return;
    setSnoozeBusy('bulk');
    try {
      for (const item of targets) {
        const res = await snoozeAttention(item.id, minutes, item.repo);
        setSnoozed((prev) => ({
          ...prev,
          [item.id]: { until: res.snoozed_until, title: item.title },
        }));
      }
      setFired(
        `Snoozed ${targets.length} item${targets.length === 1 ? '' : 's'} (${snoozePresetLabel(minutes).toLowerCase()})`,
      );
      setBulkSelected(new Set());
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSnoozeBusy(null);
    }
  }

  function toggleBulkSelect(id: string) {
    setBulkSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  useEffect(() => {
    let cancelled = false;
    if (useMock()) {
      const params = new URLSearchParams(window.location.search);
      const empty = params.get('empty') === '1';
      setData(empty ? loadInboxEmpty() : loadInbox());
      setDeferred([]);
      setPrCount(rollup ? null : loadPrs().length);
      setDeployAt(lastDeployAt(loadJobs().jobs));
      return;
    }
    // First fetch runs parallel (#897): inbox, deferred, PRs, and
    // jobs are independent reads; per-fetch errors keep the exact
    // semantics of the old sequential chain.
    (async () => {
      const [inboxResult, deferredResult, prsResult, jobsResult] =
        await Promise.allSettled([
          fetchInbox(repo),
          rollup ? Promise.resolve(null) : fetchDeferred(repo),
          rollup ? Promise.resolve(null) : fetchPrs(repo),
          fetchJobs(),
        ]);
      if (cancelled) return;
      if (inboxResult.status === 'fulfilled') {
        setData(inboxResult.value);
      } else {
        const err = inboxResult.reason;
        setError(err instanceof Error ? err.message : String(err));
      }
      if (!rollup) {
        if (deferredResult.status === 'fulfilled' && deferredResult.value) {
          setDeferred(deferredResult.value.deferred);
        } else {
          // Deferred flags load independently: strips stay empty.
          setDeferred([]);
        }
      }
      if (!rollup) {
        if (prsResult.status === 'fulfilled' && prsResult.value) {
          setPrCount(prsResult.value.prs.length);
        }
        // Hero hides a never-loaded PR count; Review shows errors.
      } else {
        setPrCount(null);
      }
      if (jobsResult.status === 'fulfilled') {
        setDeployAt(lastDeployAt(jobsResult.value.jobs));
      }
      // Hero shows no deploy age until a poll lands one.
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repo]);

  // Warm slow view reads on mount (#897, was idle): first
  // Briefing/Jobs visits land instantly instead of generating on
  // click. Fires alongside the mount fetch; cache dedup is shared,
  // so the extra call costs nothing on revisit.
  useEffect(() => {
    if (useMock() || rollup) return;
    void warmViewCaches(repo);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repo]);

  // Live queue: re-fetch on the adaptive cadence so new items land
  // without a reload. Paused in a hidden tab and while reporting.
  useEffect(() => {
    if (useMock() || !data || dialogOpen) return;
    const timer = window.setInterval(
      () => {
        if (!document.hidden) void refresh();
      },
      nowPollMs(data.attention.length),
    );
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repo, data, dialogOpen]);

  async function ensureReportLabels() {
    if (reportLabels !== null || useMock()) return;
    try {
      const r = await fetchRepoLabels(rollup ? 'remote-server' : repo);
      setReportLabels(r.labels);
    } catch {
      setReportLabels([]);
    }
  }

  async function onSubmitReport(title: string, body: string, labels: string[]) {
    setError(null);
    try {
      // Console bugs file against the console's own repo; the
      // rollup pseudo-repo is not a real checkout.
      const result = await createIssue(rollup ? 'remote-server' : repo, title, body, labels);
      setReported(result.url || 'created');
      setDialogOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  // Oriented loading skeleton (#897): same testid + role as the
  // old bare loader, but the first paint already reads as Now
  // (title + what is loading) instead of a naked ellipsis.
  if (!data && !error)
    return (
      <section data-testid="now-loading" role="status">
        <p className="row-title">Now</p>
        <p className="row-meta">Loading attention, pull requests, and jobs…</p>
      </section>
    );

  async function onRetry() {
    setError(null);
    await refresh();
  }

  const stubRows = data ? snoozedRows(snoozed, data.attention.map((i) => i.id)) : [];

  // One group member: its snoozed stub while snoozed, else the row.
  // Shared by singleton groups and accordion members so a repeat
  // snoozes exactly like a lone item.
  function renderMember(item: AttentionItem) {
    if (snoozed[item.id]) {
      return (
        <SnoozedStub
          key={item.id}
          itemId={item.id}
          title={item.title}
          until={snoozed[item.id].until}
          busy={snoozeBusy === item.id}
          onUnsnooze={() => void onUnsnooze(item.id)}
        />
      );
    }
    return (
      <AttentionRow
        key={item.id}
        item={item}
        viewRepo={repo}
        rollup={rollup}
        repoInfo={repos.find((r) => r.name === item.repo) ?? null}
        selected={bulkSelected.has(item.id)}
        busy={snoozeBusy !== null}
        onToggleSelect={() => toggleBulkSelect(item.id)}
        onDone={(title) => {
          setFired(title);
          void refresh();
        }}
        onError={setError}
        onSection={(s) => setSection(s)}
        onTab={(t) => onOpenTab(t)}
        onAskAgent={onAskAgent}
        onSnooze={(target, m) => void onSnooze(target, m)}
      />
    );
  }

  return (
    <section data-testid="now-view">
      <header className="page-head">
        <div>
          <h2>
            Now{' '}
            {data && (
              <span className="head-stamp" title={formatStamp(data.updated_at)}>
                Updated {relativeStamp(data.updated_at)}
              </span>
            )}
          </h2>
        </div>
        <button type="button" data-testid="report-bug" onClick={() => { setDialogOpen(true); void ensureReportLabels(); }}>
          Report a bug
        </button>
      </header>
      {data && (
        <NowHero
          health={health}
          feed={data}
          prCount={prCount}
          deployAt={deployAt}
          onDone={(title) => {
            setFired(title);
            void refresh();
          }}
          onError={setError}
          onSection={(s) => setSection(s)}
          onTab={(t) => onOpenTab(t)}
          onAttention={() => setSection(null)}
          onHistoryDeploys={() => {
            if (onOpenHistoryQuery) onOpenHistoryQuery('deploy');
            else onOpenTab('history');
          }}
        />
      )}
      {error && (
        <div data-testid="now-error" role="alert">
          Couldn&apos;t load what needs you: {error}{' '}
          <button type="button" data-testid="now-retry" onClick={() => void onRetry()}>
            Retry
          </button>
        </div>
      )}
      {dialogOpen && (
        <ReportDialog
          initialBody={reportContext}
          availableLabels={reportLabels ?? []}
          onSubmit={(t, b, l) => void onSubmitReport(t, b, l)}
          onCancel={() => setDialogOpen(false)}
        />
      )}
      {reported && (
        <p className="row-meta" data-testid="report-done" role="status">
          Thanks — filed{reported !== 'created' ? `: ${reported}` : '.'}
        </p>
      )}
      {fired && (
        <p className="row-meta" data-testid="now-fired" role="status">
          Done: {fired}
        </p>
      )}
      <StackSection health={health} onOpenStats={onOpenStats} />
      {data && (
        <>
          {!rollup && (
            <InboxStrips
              feed={data}
              repo={repo}
              deferred={deferred}
              onDeferredChange={setDeferred}
              onOpenReview={() => setSection('review')}
              onOpenJobs={() => setSection('jobs')}
            />
          )}
          {stubRows.length > 0 && (
            <ul className="session-list" data-testid="now-snoozed">
              {stubRows.map(([id, entry]) => (
                <SnoozedStub
                  key={id}
                  itemId={id}
                  title={entry.title}
                  until={entry.until}
                  busy={snoozeBusy === id}
                  onUnsnooze={() => void onUnsnooze(id)}
                />
              ))}
            </ul>
          )}
      {data.attention.length === 0 ? (
        <NowEmptyState onRunCheck={() => setSection('jobs')} />
      ) : (
        <>
          {bulkSelected.size > 0 && (
            <AttentionBulkBar
              count={bulkSelected.size}
              busy={snoozeBusy !== null}
              onSnooze={(m) => void onBulkSnooze(m)}
              onClear={() => setBulkSelected(new Set())}
            />
          )}
          <ul className="session-list" data-testid="now-list">
            {groupAttentionItems(data.attention).map((group, gi) =>
              group.items.length === 1 ? (
                renderMember(group.items[0])
              ) : (
                <AttentionGroupRow key={group.key} group={group} index={gi}>
                  {group.items.map((item) => renderMember(item))}
                </AttentionGroupRow>
              ),
            )}
          </ul>
        </>
      )}
          {/* Sections open from the top nav, the hero counts, the
              strips, or the palette (issue #930 removed the duplicate
              Review/Jobs/Queue toggle row that used to sit here). */}
          {section === 'review' && <ReviewView repo={repo} defaultBranch={defaultBranch} onAskAgent={onAskAgent} onSelectPr={setLastPr} createRequest={createRequest} />}
          {section === 'jobs' && <JobsView repo={repo} defaultBranch={defaultBranch} onAskAgent={onAskAgent} initialPr={lastPr} onOpenChat={() => onOpenTab('chat')} />}
          {section === 'queue' && <QueueView repo={repo} />}
        </>
      )}
    </section>
  );
}
