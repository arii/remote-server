import { useEffect, useRef, useState } from 'react';
import TapToFire from '../components/TapToFire';
import { isRollupRepo } from '../components/RepoPicker';
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

export function StackSection({ health }: { health: SystemHealthResponse | null }) {
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
          All {health.containers.length} containers up — all clear.
        </p>
      ) : (
        <ContainerPills containers={health.containers} error={health.containers_error} />
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

// Severity filter chips (issue #813): client-only narrowing of the
// attention list. Unknown severities read as info so nothing hides
// silently behind a new backend value.
export type SeverityFilter = 'crit' | 'warn' | 'info';
export const SEVERITY_FILTERS: SeverityFilter[] = ['crit', 'warn', 'info'];

export function sevKey(severity: string): SeverityFilter {
  const s = severity.toLowerCase();
  if (s === 'crit' || s === 'critical') return 'crit';
  if (s === 'warn' || s === 'warning') return 'warn';
  return 'info';
}

export function severityCounts(items: AttentionItem[]): Record<SeverityFilter, number> {
  const counts: Record<SeverityFilter, number> = { crit: 0, warn: 0, info: 0 };
  for (const item of items) counts[sevKey(item.severity)] += 1;
  return counts;
}

export function filterBySeverity(
  items: AttentionItem[],
  visible: Record<SeverityFilter, boolean>,
): AttentionItem[] {
  return items.filter((i) => visible[sevKey(i.severity)]);
}

// Snooze-all-info targets (issue #815): visible info rows not already
// collapsed behind a stub. Coordinated with #813: the caller passes
// the severity-filtered list, so hidden rows are never snoozed.
export function snoozeAllInfoTargets(
  visibleItems: AttentionItem[],
  snoozed: Record<string, SnoozedEntry>,
): AttentionItem[] {
  return visibleItems.filter((i) => sevKey(i.severity) === 'info' && !(i.id in snoozed));
}

export function SeverityChips({
  counts,
  visible,
  onToggle,
}: {
  counts: Record<SeverityFilter, number>;
  visible: Record<SeverityFilter, boolean>;
  onToggle: (sev: SeverityFilter) => void;
}) {
  return (
    <div className="actions" role="group" aria-label="Filter by severity" data-testid="now-sev-filters">
      {SEVERITY_FILTERS.map((s) => (
        <button
          key={s}
          type="button"
          data-testid={`now-sev-${s}`}
          aria-pressed={visible[s]}
          onClick={() => onToggle(s)}
        >
          <span className={sevClass(s)}>{s}</span> ({counts[s]})
        </button>
      ))}
    </div>
  );
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
      <button type="button" onClick={() => void handleGet()}>
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
    <button type="button" onClick={() => void handlePost()}>
      {item.action.label}
    </button>
  );
}

// Status hero (issue #811): one glanceable strip atop Now answering
// "is everything fine?". Every count comes from a live fetch —
// inbox, PRs, jobs, system health — never invented. Worst status
// wins: a sick container or crit item reads crit, a warn item or
// silent stack reads warn, otherwise all clear.
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
  if (sevs.has('warn') || sevs.has('warning')) return 'warn';
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

export function heroCountsText({
  attentionOpen,
  runsActive,
  prCount,
  deployAt,
}: {
  attentionOpen: number;
  runsActive: boolean;
  // Null in the rollup: open PRs are a per-repo count.
  prCount: number | null;
  // Null when no deploy run is on record.
  deployAt: number | null;
}): string {
  const parts = [
    `${attentionOpen} needing attention`,
    runsActive ? '1 active run' : 'no active runs',
  ];
  if (prCount !== null) parts.push(`${prCount} PR${prCount === 1 ? '' : 's'} open`);
  parts.push(deployAt ? `deploy ${relativeStamp(deployAt)}` : 'no recent deploys');
  return parts.join(' · ');
}

const HERO_DOT: Record<HeroStatus, string> = {
  loading: 'dot dot-idle',
  ok: 'dot dot-ok',
  warn: 'dot dot-warn',
  crit: 'dot dot-bad',
};

const HERO_LABEL: Record<HeroStatus, string> = {
  loading: 'Loading status…',
  ok: 'All clear',
  warn: 'Needs attention',
  crit: 'Action needed',
};

export function NowHero({
  health,
  feed,
  prCount,
  deployAt,
  onDone,
  onError,
  onSection,
  onTab,
}: {
  health: SystemHealthResponse | null;
  feed: InboxFeed;
  prCount: number | null;
  deployAt: number | null;
  onDone: (title: string) => void;
  onError: (message: string) => void;
  onSection: (section: 'review' | 'jobs') => void;
  onTab: (tab: AttentionNavTab) => void;
}) {
  const status = heroStatus(health, feed.attention);
  const top = topUrgent(feed.attention);
  const counts = heroCountsText({
    attentionOpen: feed.attention.length,
    runsActive: feed.runs.active !== null,
    prCount,
    deployAt,
  });
  return (
    <section data-testid="now-hero" aria-label="Status summary" className="health now-hero">
      <span aria-hidden="true" data-testid="now-hero-dot" className={HERO_DOT[status]} />
      <strong data-testid="now-hero-status">{HERO_LABEL[status]}</strong>
      <span className="row-meta" data-testid="now-hero-counts">
        {counts}
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

export default function NowView({
  repo,
  defaultBranch,
  health,
  reportContext,
  onAskAgent,
  onOpenTab,
  sectionRequest = null,
  reportRequest = 0,
}: {
  repo: string;
  // Checkout branch of the viewed repo, seeding the Review/Jobs
  // branch fields (#708).
  defaultBranch?: string;
  health: SystemHealthResponse | null;
  reportContext: string;
  onAskAgent: (seed: string) => void;
  onOpenTab: (tab: AttentionNavTab) => void;
  // Command-palette requests (issue #810): nonce bumps from App that
  // open a section or the report dialog, including on a fresh mount
  // after the palette navigates here from another tab.
  sectionRequest?: { section: 'review' | 'jobs'; nonce: number } | null;
  reportRequest?: number;
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
  const [section, setSection] = useState<'review' | 'jobs' | null>(null);
  // Last Review row selection, surviving section switches so Jobs can
  // prefill its PR field from row context. Reset on repo change.
  const [lastPr, setLastPr] = useState<number | null>(null);
  // Optimistic snooze stubs by item id. A stub collapses its card
  // instantly; refresh() reconciles against the server list.
  const [snoozed, setSnoozed] = useState<Record<string, SnoozedEntry>>({});
  const [snoozeBusy, setSnoozeBusy] = useState<string | null>(null);
  // Bulk snooze (issue #815) runs its own in-flight flag: the single
  // id stays the single-snooze contract, untouched.
  const [snoozeAllBusy, setSnoozeAllBusy] = useState(false);
  // Severity filter (issue #813): all visible by default.
  const [visibleSevs, setVisibleSevs] = useState<Record<SeverityFilter, boolean>>({
    crit: true,
    warn: true,
    info: true,
  });
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
    if (snoozeBusy !== null || snoozeAllBusy) return;
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
    if (snoozeBusy !== null || snoozeAllBusy) return;
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

  // One-tap snooze-all-info (issue #815): same per-item endpoint and
  // stub semantics as single snooze, run sequentially over the
  // visible info rows with one terminal refresh. Successes keep
  // their stubs; each failure drops its stub and names its row.
  async function onSnoozeAllInfo(targets: AttentionItem[]) {
    if (snoozeBusy !== null || snoozeAllBusy || targets.length === 0) return;
    setSnoozeAllBusy(true);
    const failed: string[] = [];
    let ok = 0;
    try {
      for (const item of targets) {
        const optimisticUntil = Date.now() / 1000 + 60 * 60;
        setSnoozed((prev) => ({
          ...prev,
          [item.id]: { until: optimisticUntil, title: item.title },
        }));
        try {
          const res = await snoozeAttention(item.id, 60, item.repo);
          setSnoozed((prev) => ({
            ...prev,
            [item.id]: { until: res.snoozed_until, title: item.title },
          }));
          ok += 1;
        } catch {
          dropStub(item.id);
          failed.push(item.title);
        }
      }
      const noun = ok === 1 ? 'info item' : 'info items';
      if (ok > 0 && failed.length === 0) setFired(`Snoozed ${ok} ${noun} for 1h`);
      else if (ok > 0) setFired(`Snoozed ${ok} ${noun} for 1h; failed: ${failed.join(', ')}`);
      else setError(`Couldn't snooze info rows: ${failed.join(', ')}`);
      await refresh();
    } finally {
      setSnoozeAllBusy(false);
    }
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
    (async () => {
      try {
        const live = await fetchInbox(repo);
        if (!cancelled) setData(live);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
      if (!rollup) {
        try {
          const d = await fetchDeferred(repo);
          if (!cancelled) setDeferred(d.deferred);
        } catch {
          // Deferred flags load independently: strips stay empty.
          if (!cancelled) setDeferred([]);
        }
      }
      if (!rollup) {
        try {
          const prs = await fetchPrs(repo);
          if (!cancelled) setPrCount(prs.prs.length);
        } catch {
          // Hero hides a never-loaded PR count; Review shows errors.
        }
      } else if (!cancelled) {
        setPrCount(null);
      }
      try {
        const jobs = await fetchJobs();
        if (!cancelled) setDeployAt(lastDeployAt(jobs.jobs));
      } catch {
        // Hero shows no deploy age until a poll lands one.
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repo]);

  // Warm slow view reads on idle so first Briefing/Jobs visits land
  // instantly instead of generating on click.
  useEffect(() => {
    if (useMock() || rollup) return;
    const warm = () => void warmViewCaches(repo);
    if (typeof window.requestIdleCallback === 'function') {
      const id = window.requestIdleCallback(warm);
      return () => window.cancelIdleCallback(id);
    }
    const t = window.setTimeout(warm, 0);
    return () => window.clearTimeout(t);
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

  if (!data && !error) return <section data-testid="now-loading" role="status">Loading…</section>;

  async function onRetry() {
    setError(null);
    await refresh();
  }

  const stubRows = data ? snoozedRows(snoozed, data.attention.map((i) => i.id)) : [];
  // Severity-narrowed list (issue #813): chips + hero counts read the
  // full feed; only the row list narrows. All-off lands on the empty
  // state with the chips still up so the operator can toggle back.
  const visibleItems = data ? filterBySeverity(data.attention, visibleSevs) : [];
  const infoTargets = data ? snoozeAllInfoTargets(visibleItems, snoozed) : [];
  const snoozeAnyBusy = snoozeBusy !== null || snoozeAllBusy;

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
      <StackSection health={health} />
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
                  busy={snoozeBusy === id || snoozeAllBusy}
                  onUnsnooze={() => void onUnsnooze(id)}
                />
              ))}
            </ul>
          )}
      {data.attention.length > 0 && (
        <>
          <SeverityChips
            counts={severityCounts(data.attention)}
            visible={visibleSevs}
            onToggle={(s) => setVisibleSevs((v) => ({ ...v, [s]: !v[s] }))}
          />
          <div className="actions">
            <button
              type="button"
              data-testid="now-snooze-all-info"
              disabled={infoTargets.length === 0 || snoozeAnyBusy}
              title={infoTargets.length === 0 ? 'No visible info rows to snooze' : `Snooze ${infoTargets.length} visible info rows for 1h`}
              onClick={() => void onSnoozeAllInfo(infoTargets)}
            >
              {snoozeAllBusy ? 'Snoozing…' : `Snooze all info (${infoTargets.length})`}
            </button>
          </div>
        </>
      )}
      {visibleItems.length === 0 ? (
        <NowEmptyState onRunCheck={() => setSection('jobs')} />
      ) : (
        <ul className="session-list" data-testid="now-list">
          {visibleItems.map((item) =>
            snoozed[item.id] ? (
              <SnoozedStub
                key={item.id}
                itemId={item.id}
                title={item.title}
                until={snoozed[item.id].until}
                busy={snoozeBusy === item.id || snoozeAllBusy}
                onUnsnooze={() => void onUnsnooze(item.id)}
              />
            ) : (
            <li key={item.id} className="detail" data-testid={`now-item-${item.id}`}>
              {showRepoTag(rollup, item.repo, repo) ? (
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
              <p>{item.detail}</p>
              <div className="actions">
                <ActionControl
                  item={item}
                  onDone={(title) => {
                    setFired(title);
                    void refresh();
                  }}
                  onError={setError}
                  onSection={(s) => setSection(s)}
                  onTab={(t) => onOpenTab(t)}
                />
                <button
                  type="button"
                  data-testid={`ask-agent-${item.id}`}
                  onClick={() => onAskAgent(askAgentSeed(item))}
                >
                  Ask agent
                </button>
                <SnoozeButtons
                  itemId={item.id}
                  busy={snoozeAnyBusy}
                  onSnooze={(m) => void onSnooze(item, m)}
                />
              </div>
            </li>
            ),
          )}
        </ul>
      )}
      {!rollup && (
        <div className="actions" data-testid="inbox-sections">
          <button
            type="button"
            data-testid="inbox-open-review"
            aria-pressed={section === 'review'}
            onClick={() => setSection((s) => (s === 'review' ? null : 'review'))}
          >
            Review
          </button>
          <button
            type="button"
            data-testid="inbox-open-jobs"
            aria-pressed={section === 'jobs'}
            onClick={() => setSection((s) => (s === 'jobs' ? null : 'jobs'))}
          >
            Jobs{data.runs.active ? ' •' : ''}
          </button>
        </div>
      )}
          {section === 'review' && <ReviewView repo={repo} defaultBranch={defaultBranch} onAskAgent={onAskAgent} onSelectPr={setLastPr} />}
          {section === 'jobs' && <JobsView repo={repo} defaultBranch={defaultBranch} onAskAgent={onAskAgent} initialPr={lastPr} />}
        </>
      )}
    </section>
  );
}
