import { useEffect, useMemo, useState } from 'react';
import NowView from './views/NowView';
import ChatView from './views/ChatView';
import BriefingView from './views/BriefingView';
import HistoryView, { type HistoryFilterRequest } from './views/HistoryView';
import RepoPicker, { ALL_REPOS, isRollupRepo } from './components/RepoPicker';
import SyncBanner from './components/SyncBanner';
import CommandPalette from './components/CommandPalette';
import { buildCommands, type PaletteCommand } from './components/commandRegistry';
import {
  fetchContainerStats,
  fetchDispatchQueue,
  fetchInbox,
  fetchJobs,
  fetchLedger,
  fetchPrs,
  fetchRepoDiff,
  fetchRepos,
  fetchSystemHealth,
  flushOutbox,
  prefetchTabPayloads,
  repoAction,
  restartContainer,
  startJobRun,
  subscribeApiFailures,
  warmRepoMetrics,
} from './apiClient';
import type { ContainerStatsRow, RepoAction, RepoActionResponse, SystemHealthResponse } from './apiClient';
import type { RepoInfo } from './reviewTypes';
import {
  loadContainerStats,
  loadDispatchQueue,
  loadHistory,
  loadJobs,
  loadPrs,
  loadRepoDiff,
  loadRepos,
  loadSystemHealth,
  useMock,
} from './views/mockViews';
import { loadInbox } from './mocks/inbox';
import {
  EMPTY_BADGE_COUNTS,
  historyErrorCount,
  jobsActiveCount,
  navBadge,
  nowBadgeCount,
  queueActiveCount,
  reviewBadgeCount,
} from './tabBadges';
import type { NavBadgeCounts } from './tabBadges';
import RepoDiffModal from './views/RepoDiffModal';
import type { RepoDiffData } from './views/RepoDiffModal';
import ContainerDrawer, { mergeStatsRows, restartActionFor, serviceRestartable, statsRowsFromHealth } from './views/ContainerDrawer';
import ContainerLogsDrawer from './views/ContainerLogsDrawer';
import { dangerEntryFor } from './views/DangerZone';
import { clickChatSend, useAppShortcuts } from './shortcuts';
import { ShortcutCheatsheet, ShortcutHint } from './components/ShortcutCheatsheet';

// Stack root for Terminal/Code links: relative in production (same host),
// VITE_STACK_URL override for `vite dev` on a port without those routes.
function stackBase(): string {
  return import.meta.env.VITE_STACK_URL ?? '';
}

// Header health revalidation cadence (issue #588): slow enough to
// never storm, fast enough that outages surface within a minute.
export const HEALTH_POLL_MS = 60_000;

// Single-in-flight async trigger: concurrent calls collapse into
// the in-flight run, so failure bursts can't stack refreshes. The
// guard releases on resolve and on reject alike.
export function singleFlight(fn: () => Promise<void>): () => void {
  let inflight = false;
  return () => {
    if (inflight) return;
    inflight = true;
    void fn().then(
      () => { inflight = false; },
      () => { inflight = false; },
    );
  };
}

function healthSummary(health: SystemHealthResponse | null): string {
  if (!health) return 'Status: loading…';
  if (health.containers_error) return 'Status: container list unavailable';
  const up = health.containers.filter((c) => c.state === 'running').length;
  return `Status: ${up}/${health.containers.length} containers up`;
}

export type Tab = 'now' | 'chat' | 'briefing' | 'history';

// Persistent console nav (#896): one bar, seven destinations. Review,
// Jobs, and Queue live inside Now as sections (not routes); the nav
// reaches them through the palette's section-request path. Queue
// joined the bar in #930 (it was orphaned: reachable only from the
// duplicate toggle row that issue removed).
export type NavEntry =
  | { kind: 'tab'; id: Tab; label: string; key: string }
  | { kind: 'section'; id: 'review' | 'jobs' | 'queue'; label: string };

export const NAV_ENTRIES: NavEntry[] = [
  { kind: 'tab', id: 'now', label: 'Now', key: '1' },
  { kind: 'tab', id: 'chat', label: 'Chat', key: '2' },
  { kind: 'tab', id: 'briefing', label: 'Briefing', key: '3' },
  { kind: 'tab', id: 'history', label: 'History', key: '4' },
  { kind: 'section', id: 'review', label: 'Review' },
  { kind: 'section', id: 'jobs', label: 'Jobs' },
  { kind: 'section', id: 'queue', label: 'Queue' },
];

export type NowSection = 'review' | 'jobs' | 'queue' | null;

// Pressed rule for the unified nav: tabs light on tab match, except
// Now yields to an open section entry; Now stays lit for the inbox
// only (every section has an entry since #930).
export function navPressed(tab: Tab, section: NowSection, entry: NavEntry): boolean {
  if (entry.kind === 'tab') {
    if (tab !== entry.id) return false;
    if (entry.id !== 'now') return true;
    return section !== 'review' && section !== 'jobs' && section !== 'queue';
  }
  return tab === 'now' && section === entry.id;
}

const FALLBACK_REPOS: RepoInfo[] = [
  { name: 'remote-server', full: 'arii/remote-server', branch: 'main', clean: true },
];

// Rollup guard (issue #610): Chat/Briefing/History are per-repo
// views with no `repo=all` API; prompt for a repo instead of
// firing per-repo fetches at a pseudo-repo.
export function RollupGate({ view }: { view: string }) {
  return (
    <section data-testid="rollup-gate" role="status">
      <p className="row-meta">
        {view} needs one repo — pick one above to leave the All repos rollup.
      </p>
    </section>
  );
}

export default function App() {
  const [tab, setTab] = useState<Tab>('now');
  const [repos, setRepos] = useState<RepoInfo[]>(FALLBACK_REPOS);
  const [repo, setRepo] = useState('remote-server');
  const [health, setHealth] = useState<SystemHealthResponse | null>(null);
  const [chatSeed, setChatSeed] = useState<string | null>(null);
  // Header drawers (issue #663): dirty-repo diff modal + container
  // health drawer, both inline under the toolbar while open.
  const [diffRepo, setDiffRepo] = useState<string | null>(null);
  const [diffData, setDiffData] = useState<RepoDiffData | null>(null);
  const [diffError, setDiffError] = useState<string | null>(null);
  const [statsOpen, setStatsOpen] = useState(false);
  const [statsRows, setStatsRows] = useState<ContainerStatsRow[]>([]);
  const [statsLoading, setStatsLoading] = useState(false);
  const [statsError, setStatsError] = useState<string | null>(null);
  const [statsDegraded, setStatsDegraded] = useState<string | null>(null);
  const [statsSeq, setStatsSeq] = useState(0);
  const [restartRequested, setRestartRequested] = useState<string | null>(null);
  const [serviceRestarted, setServiceRestarted] = useState<string | null>(null);
  const [restartError, setRestartError] = useState<string | null>(null);
  // Log slide-over selection (issue #928): which container's tail
  // is open atop the stats drawer, if any.
  const [logService, setLogService] = useState<string | null>(null);
  // Cross-tab feed filter (issue #931): a container pill in Now or
  // the Status drawer navigates to History pre-filtered.
  const [historyFilter, setHistoryFilter] = useState<HistoryFilterRequest | null>(null);
  // Command palette (issue #810): open flag plus one nonce per
  // cross-view request, so a command lands even when its view is
  // already mounted (or mounts fresh on the tab switch).
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [sectionRequest, setSectionRequest] = useState<{
    section: 'review' | 'jobs' | 'queue' | null;
    nonce: number;
  } | null>(null);
  // Open Now section mirrored up from NowView (#896): drives the
  // nav pressed state for the Review/Jobs entries.
  const [nowSection, setNowSection] = useState<NowSection>(null);
  const [reportRequest, setReportRequest] = useState(0);
  const [chatFocusRequest, setChatFocusRequest] = useState(0);
  const [briefingReloadRequest, setBriefingReloadRequest] = useState(0);
  const [createRequest, setCreateRequest] = useState(0);
  // History deep-link search seed (issue #930): the Now hero's
  // deploy segment opens History filtered to deploy runs. Nonce
  // path like sectionRequest — History stays mounted (keepalive),
  // so a bump re-seeds even on revisit.
  const [historyQueryRequest, setHistoryQueryRequest] = useState<{
    query: string;
    nonce: number;
  } | null>(null);

  // Cheatsheet visibility for the app-level shortcuts (#808). Tab/send
  // shortcuts suspend while a modal/drawer owns the screen; the
  // cheatsheet and the palette stay reachable. Ctrl+K toggles the
  // palette through this one listener (#902) — no second keydown
  // subscription, so the chord can never double-fire.
  const [helpOpen, setHelpOpen] = useState(false);
  useAppShortcuts({
    suspended: diffRepo !== null || statsOpen,
    helpOpen,
    onTab: setTab,
    onSend: () => clickChatSend(),
    onToggleHelp: () => setHelpOpen((v) => !v),
    onCloseHelp: () => setHelpOpen(false),
    onPalette: () => setPaletteOpen((o) => !o),
  });

  function askAgent(seed: string) {
    setChatSeed(seed);
    setTab('chat');
  }

  // Leaving Now unmounts it (its section resets to the inbox on
  // return); mirror that here so the pressed state never goes stale.
  useEffect(() => {
    if (tab !== 'now') setNowSection(null);
  }, [tab]);

  // Unified nav handler (#896): tabs switch; Now also closes any
  // open section (inbox home); Review/Jobs ride the section-request
  // path like their palette commands.
  function goNav(entry: NavEntry) {
    if (entry.kind === 'tab' && entry.id !== 'now') {
      setTab(entry.id);
      return;
    }
    setTab('now');
    const section = entry.kind === 'section' ? entry.id : null;
    setSectionRequest((r) => ({ section, nonce: (r?.nonce ?? 0) + 1 }));
  }

  const commands = useMemo(
    () =>
      buildCommands({
        go: (t) => setTab(t),
        openSection: (s) =>
          setSectionRequest((r) => ({ section: s, nonce: (r?.nonce ?? 0) + 1 })),
        reportBug: () => setReportRequest((n) => n + 1),
        focusChat: () => setChatFocusRequest((n) => n + 1),
        refreshBriefing: () => setBriefingReloadRequest((n) => n + 1),
        switchRepo: (name) => setRepo(name),
        syncNow: () => void flushOutbox(),
        openStats: () => openStats(),
        newIssue: () => setCreateRequest((n) => n + 1),
        openUrl: (url) => {
          window.location.href = url;
        },
        terminalUrl: `${stackBase()}/`,
        codeUrl: `${stackBase()}/code`,
        repos,
        repo,
      }),
    [repos, repo],
  );

  function runCommand(cmd: PaletteCommand) {
    setPaletteOpen(false);
    // Section/report/chat/briefing commands navigate first: the nonce
    // effects in the target view fire on the tab switch.
    if (
      cmd.id === 'open-review' ||
      cmd.id === 'open-jobs' ||
      cmd.id === 'open-queue' ||
      cmd.id === 'report-bug' ||
      cmd.id === 'new-issue'
    ) {
      setTab('now');
    } else if (cmd.id === 'focus-chat') {
      setTab('chat');
    } else if (cmd.id === 'refresh-briefing') {
      setTab('briefing');
    }
    cmd.run();
  }

  useEffect(() => {
    if (useMock()) {
      const r = loadRepos();
      setRepos(r);
      if (repo !== ALL_REPOS && !r.some((x) => x.name === repo) && r.length > 0)
        setRepo(r[0].name);
      setHealth(loadSystemHealth());
      return;
    }
    // Repos + health fetch in parallel (#897): independent reads,
    // per-fetch errors preserved.
    (async () => {
      const [reposResult, healthResult] = await Promise.allSettled([
        fetchRepos(),
        fetchSystemHealth(),
      ]);
      if (reposResult.status === 'fulfilled') {
        const live = reposResult.value;
        if (live.repos.length > 0) {
          setRepos(live.repos);
          if (repo !== ALL_REPOS && !live.repos.some((x) => x.name === repo))
            setRepo(live.repos[0].name);
        }
      }
      // Keep fallback repo; header still renders the picker.
      if (healthResult.status === 'fulfilled') setHealth(healthResult.value);
      // Header pill stays in loading state; History shows the error.
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Revalidate the header snapshot past mount (issue #588): slow
  // poll plus a refresh on any API failure, so the pill stops
  // reading stale all-clear through outages. Single in-flight;
  // failures keep the last good snapshot and retry next tick.
  useEffect(() => {
    if (useMock()) return;
    let cancelled = false;
    const refresh = singleFlight(async () => {
      try {
        const next = await fetchSystemHealth();
        if (!cancelled) setHealth(next);
      } catch {
        // Keep the last good snapshot; the next tick retries.
      }
    });
    const timer = window.setInterval(refresh, HEALTH_POLL_MS);
    const unsubscribe = subscribeApiFailures(refresh);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      unsubscribe();
    };
  }, []);

  // Diff modal fetch: on open + after each one-click git action
  // (diffSeq); the modal is otherwise a point-in-time read (close
  // + reopen retries).
  const [diffSeq, setDiffSeq] = useState(0);
  useEffect(() => {
    if (!diffRepo) return;
    let cancelled = false;
    setDiffData(null);
    setDiffError(null);
    (async () => {
      try {
        const d = useMock() ? loadRepoDiff(diffRepo) : await fetchRepoDiff(diffRepo);
        if (cancelled) return;
        setDiffData({
          statusShort: d.status_short,
          statusTruncated: d.status_truncated,
          diffStat: d.diff_stat,
          diff: d.diff,
          truncated: d.truncated,
          files: d.files,
          filesTruncated: d.files_truncated,
        });
      } catch (err) {
        if (!cancelled) setDiffError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [diffRepo, diffSeq]);

  // Per-repo git action for the dirty-repos strip (#937): the row's
  // repo rides the request, so the global picker never moves first.
  // Refreshes the header snapshot so every strip row re-reads.
  async function onRepoActionFor(
    name: string,
    action: RepoAction,
  ): Promise<RepoActionResponse> {
    const r = await repoAction(name, action);
    try {
      const live = await fetchRepos();
      if (live.repos.length > 0) setRepos(live.repos);
    } catch {
      // Strip keeps its prior rows; the row shows the result.
    }
    return r;
  }

  // Open in Chat from the strip (#937): the one strip action that
  // navigates — it selects the row's repo and opens Chat there.
  function openRepoInChat(name: string) {
    setRepo(name);
    setChatSeed(null);
    setTab('chat');
  }

  // One-click git action from the diff modal (#927): run it, then
  // refresh the modal payload and the header dirty pill.
  async function onRepoAction(action: RepoAction): Promise<RepoActionResponse> {
    if (!diffRepo) throw new Error('no repo selected');
    const r = await repoAction(diffRepo, action);
    setDiffSeq((n) => n + 1);
    try {
      const live = await fetchRepos();
      if (live.repos.length > 0) setRepos(live.repos);
    } catch {
      // Pill keeps its prior state; the modal shows the result.
    }
    return r;
  }

  // Stats drawer fetch (issue #928): rows always derive from the
  // header health snapshot (the History banner's store) with live
  // stats overlaid; a stats failure degrades to health rows with a
  // note instead of blanking the drawer on a 404. Refreshes on
  // open + manual refresh (statsSeq) + health arrival, never a
  // ticker — point-in-time CPU/RAM per the issue.
  useEffect(() => {
    if (!statsOpen) return;
    let cancelled = false;
    setStatsLoading(true);
    setStatsError(null);
    setStatsDegraded(null);
    const base = statsRowsFromHealth(health);
    (async () => {
      try {
        const s = useMock() ? loadContainerStats() : await fetchContainerStats();
        if (cancelled) return;
        setStatsRows(mergeStatsRows(base, s.containers));
      } catch (err) {
        if (cancelled) return;
        if (base.length > 0) {
          setStatsRows(base);
          setStatsDegraded(
            `Live CPU/RAM unavailable — ${err instanceof Error ? err.message : String(err)}`,
          );
        } else {
          setStatsError(err instanceof Error ? err.message : String(err));
        }
      } finally {
        if (!cancelled) setStatsLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statsOpen, statsSeq, health]);

  async function onRestartContainer(action: string) {
    // Same engine path + confirm text as DangerZone restarts; the
    // drawer only offers actions with a DangerZone entry.
    setRestartError(null);
    try {
      await startJobRun(action, repo, true);
      setRestartRequested(action);
    } catch (err) {
      setRestartError(err instanceof Error ? err.message : String(err));
    }
  }

  async function onRestartServiceContainer(service: string) {
    // Plain compose restart for services without an engine action.
    setRestartError(null);
    try {
      await restartContainer(service);
      setServiceRestarted(service);
    } catch (err) {
      setRestartError(err instanceof Error ? err.message : String(err));
    }
  }

  // One restart entry per service: the engine action where one
  // exists (buttons/code-server), else the plain system restart.
  function restartForService(service: string) {
    const action = restartActionFor(service);
    if (action) void onRestartContainer(action);
    else void onRestartServiceContainer(service);
  }

  function closeStats() {
    setStatsOpen(false);
    setRestartRequested(null);
    setServiceRestarted(null);
    setRestartError(null);
    setStatsDegraded(null);
    setLogService(null);
  }

  // Status pill / container pill entry: opens the drawer, focusing
  // the log slide-over on the pill's container when one was given.
  function openStats(service?: string) {
    setLogService(service || null);
    setStatsOpen(true);
  }

  // Container pill History action (issue #931): the History tab
  // owns the chronological feed, so the pill navigates there with
  // the feed pre-filtered to the pill's service.
  function openHistoryFilter(service: string) {
    setHistoryFilter((r) => ({ query: service, facet: 'all', nonce: (r?.nonce ?? 0) + 1 }));
    setTab('history');
  }

  // Prefetch repo metrics on select (issue #897): fire-and-forget
  // into the view cache so the Chat strip renders instantly with
  // zero taps and zero model turns. Skipped for the rollup pseudo
  // repo (Chat never mounts there — RollupGate).
  useEffect(() => {
    if (useMock() || isRollupRepo(repo)) return;
    void warmRepoMetrics(repo);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repo]);

  // Live nav badges + background precache (issue #936, review
  // F1 on #954): every counter derives from the same payload its
  // destination renders. Boot + repo switch run a precache plus an
  // immediate refresh; a 60s poll, a tab-visible refresh, and a
  // refresh on any API failure keep the pills live afterwards —
  // single-flight, sharing the view cache, so each tick costs one
  // request per endpoint. Null means not loaded yet — the pill
  // stays hidden instead of flashing a wrong zero, and a failed
  // refresh keeps the last good counts.
  const [badgeCounts, setBadgeCounts] = useState<NavBadgeCounts>(EMPTY_BADGE_COUNTS);
  useEffect(() => {
    if (useMock()) {
      const feed = loadInbox();
      const jobs = loadJobs().jobs;
      const history = loadHistory();
      setBadgeCounts({
        now: nowBadgeCount(feed),
        review: reviewBadgeCount(loadPrs()),
        history: historyErrorCount(history.jobs, history.entries),
        jobs: jobsActiveCount(jobs),
        queue: queueActiveCount(loadDispatchQueue().items),
      });
      return;
    }
    let cancelled = false;
    const refresh = singleFlight(async () => {
      const rollup = isRollupRepo(repo);
      const [inboxRes, prsRes, jobsRes, ledgerRes, queueRes] = await Promise.allSettled([
        fetchInbox(repo),
        rollup ? Promise.resolve(null) : fetchPrs(repo),
        fetchJobs(),
        fetchLedger('all', 100),
        fetchDispatchQueue(),
      ]);
      if (cancelled) return;
      setBadgeCounts((prev) => ({
        now: inboxRes.status === 'fulfilled' && inboxRes.value
          ? nowBadgeCount(inboxRes.value)
          : prev.now,
        review: prsRes.status === 'fulfilled'
          ? (prsRes.value ? reviewBadgeCount(prsRes.value.prs) : null)
          : prev.review,
        history: jobsRes.status === 'fulfilled' && ledgerRes.status === 'fulfilled'
          ? historyErrorCount(jobsRes.value.jobs, ledgerRes.value.entries)
          : prev.history,
        jobs: jobsRes.status === 'fulfilled' ? jobsActiveCount(jobsRes.value.jobs) : prev.jobs,
        queue: queueRes.status === 'fulfilled'
          ? queueActiveCount(queueRes.value.items)
          : prev.queue,
      }));
    });
    void prefetchTabPayloads(repo);
    refresh();
    const timer = window.setInterval(refresh, HEALTH_POLL_MS);
    const onVisible = () => {
      if (document.visibilityState === 'visible') refresh();
    };
    document.addEventListener('visibilitychange', onVisible);
    const unsubscribe = subscribeApiFailures(refresh);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
      unsubscribe();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repo]);

  // Keep-alive tabs (issue #897): Briefing + History mount on
  // first visit and stay mounted hidden afterwards, so warm
  // switches render with zero remount and zero network. Now keeps
  // remounting (its section resets to the inbox on return); Chat
  // keeps remounting (#573 persistence restores the conversation).
  const [seenTabs, setSeenTabs] = useState<Set<string>>(() => new Set(['now']));
  useEffect(() => {
    setSeenTabs((prev) => (prev.has(tab) ? prev : new Set(prev).add(tab)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab]);

  const selectedRepo = repos.find((r) => r.name === repo) ?? null;
  const diffBranch = diffRepo ? (repos.find((r) => r.name === diffRepo)?.branch ?? '') : '';
  const logRow = logService ? (statsRows.find((r) => r.service === logService) ?? null) : null;
  const logUsage = logRow
    ? `${logRow.state} · ${(logRow.status ?? '').trim() || 'uptime unknown'} · CPU ${logRow.cpu_pct === null ? '—' : `${logRow.cpu_pct}%`} · RAM ${logRow.mem_used && logRow.mem_limit ? `${logRow.mem_used} / ${logRow.mem_limit}` : '—'}`
    : '';
  const logEngineAction = logService ? restartActionFor(logService) : null;
  const logRestartNote = !logService
    ? null
    : logEngineAction && restartRequested === logEngineAction
      ? 'restart requested — watch Jobs for the run.'
      : serviceRestarted === logService
        ? 'restart requested — the container is restarting.'
        : null;

  return (
    <main className="layout">
      <h1 className="sr-only">Operator console</h1>
      <header className="toolbar">
        <RepoPicker repos={repos} value={repo} onChange={setRepo} />
        {selectedRepo && !selectedRepo.clean && (
          <button
            type="button"
            className="repo-chip"
            data-testid="repo-diff-chip"
            onClick={() => setDiffRepo(repo)}
          >
            dirty · view diff
          </button>
        )}
        <button
          type="button"
          className="repo-chip"
          data-testid="stack-status"
          aria-expanded={statsOpen}
          onClick={() => (statsOpen ? closeStats() : openStats())}
        >
          {healthSummary(health)}
        </button>
        <span className="spacer" />
        <button
          type="button"
          data-testid="palette-open"
          title="Open command palette (Ctrl+K)"
          onClick={() => setPaletteOpen(true)}
        >
          Commands
        </button>
        <a data-testid="tool-terminal" href={`${stackBase()}/`}>
          Terminal
        </a>
        <a data-testid="tool-code" href={`${stackBase()}/code`}>
          Code
        </a>
      </header>
      <SyncBanner />
      <CommandPalette
        open={paletteOpen}
        commands={commands}
        onClose={() => setPaletteOpen(false)}
        onRun={runCommand}
      />
      {diffRepo && (
        <RepoDiffModal
          repo={diffRepo}
          branch={diffBranch}
          data={diffData}
          error={diffError}
          onAction={(action) => onRepoAction(action)}
          onAskAgent={askAgent}
          onClose={() => setDiffRepo(null)}
        />
      )}
      {statsOpen && (
        <ContainerDrawer
          rows={statsRows}
          loading={statsLoading}
          error={statsError}
          degraded={statsDegraded}
          restartRequested={restartRequested}
          restartError={restartError}
          serviceRestarted={serviceRestarted}
          diskSummary={health?.disk.summary ?? null}
          memSummary={health?.memory.summary ?? null}
          onRestart={(action) => void onRestartContainer(action)}
          onRestartService={(service) => void onRestartServiceContainer(service)}
          onOpenLogs={(service) => setLogService(service)}
          onFilterHistory={openHistoryFilter}
          onRefresh={() => setStatsSeq((n) => n + 1)}
          onClose={closeStats}
        />
      )}
      {statsOpen && logService && (
        <ContainerLogsDrawer
          service={logService}
          name={logRow?.name ?? logService}
          usage={logUsage}
          canRestart={logEngineAction !== null || serviceRestartable(logService)}
          restartConfirm={
            logEngineAction ? dangerEntryFor(logEngineAction)?.confirm : undefined
          }
          restartNote={logRestartNote}
          restartError={restartError}
          onRestart={restartForService}
          onClose={() => setLogService(null)}
        />
      )}
      <nav className="topnav" aria-label="Views">
        {NAV_ENTRIES.map((t) => {
          // Live count pill (issue #936): hidden until the first
          // refresh lands and whenever the count is zero, so the bar
          // only speaks up when a destination needs the operator.
          const badge = navBadge(t.id, badgeCounts);
          return (
            <button
              key={t.id}
              type="button"
              data-testid={`nav-${t.id}`}
              aria-pressed={navPressed(tab, nowSection, t)}
              aria-keyshortcuts={t.kind === 'tab' ? t.key : undefined}
              title={t.kind === 'tab' ? `${t.label} (${t.key})` : t.label}
              onClick={() => goNav(t)}
            >
              {t.label}
              {badge && (
                <span
                  className="nav-badge"
                  data-testid={`nav-badge-${t.id}`}
                  title={badge.label}
                  aria-label={badge.label}
                >
                  {badge.count}
                </span>
              )}
            </button>
          );
        })}
      </nav>
      <ShortcutHint />
      {tab === 'now' && (
        <NowView
          repo={repo}
          defaultBranch={repos.find((r) => r.name === repo)?.branch}
          repos={repos}
          health={health}
          reportContext={`${isRollupRepo(repo) ? 'All repos' : `Repo ${repo}`} · ${healthSummary(health)}`}
          onAskAgent={askAgent}
          onOpenTab={(t) => setTab(t)}
          onOpenHistoryQuery={(q) => {
            setHistoryQueryRequest((r) => ({ query: q, nonce: (r?.nonce ?? 0) + 1 }));
            setTab('history');
          }}
          sectionRequest={sectionRequest}
          reportRequest={reportRequest}
          createRequest={createRequest}
          onSectionChange={setNowSection}
          onOpenStats={openStats}
          onViewDiff={(name) => setDiffRepo(name)}
          onRepoAction={(name, action) => onRepoActionFor(name, action)}
          onOpenChat={openRepoInChat}
          onFilterHistory={openHistoryFilter}
        />
      )}
      {tab === 'chat' &&
        (isRollupRepo(repo) ? (
          <RollupGate view="Chat" />
        ) : (
          <ChatView
            repo={repo}
            seed={chatSeed}
            onSeedUsed={() => setChatSeed(null)}
            focusRequest={chatFocusRequest}
            onOpenDiff={() => setDiffRepo(repo)}
          />
        ))}
      {tab === 'briefing' && isRollupRepo(repo) && <RollupGate view="Briefing" />}
      {seenTabs.has('briefing') && !isRollupRepo(repo) && (
        <div hidden={tab !== 'briefing'} data-testid="keepalive-briefing">
          <BriefingView
            repo={repo}
            onAskAgent={askAgent}
            onOpenHistory={() => setTab('history')}
            onOpenReview={() => {
              setTab('now');
              setSectionRequest((r) => ({ section: 'review', nonce: (r?.nonce ?? 0) + 1 }));
            }}
            reloadRequest={briefingReloadRequest}
            active={tab === 'briefing'}
          />
        </div>
      )}
      {tab === 'history' && isRollupRepo(repo) && <RollupGate view="History" />}
      {seenTabs.has('history') && !isRollupRepo(repo) && (
        <div hidden={tab !== 'history'} data-testid="keepalive-history">
          <HistoryView
            repo={repo}
            active={tab === 'history'}
            queryRequest={historyQueryRequest}
            filterRequest={historyFilter}
          />
        </div>
      )}
      <ShortcutCheatsheet open={helpOpen} onClose={() => setHelpOpen(false)} />
    </main>
  );
}
