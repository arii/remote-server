import { useEffect, useMemo, useState } from 'react';
import NowView from './views/NowView';
import ChatView from './views/ChatView';
import BriefingView from './views/BriefingView';
import HistoryView from './views/HistoryView';
import RepoPicker, { ALL_REPOS, isRollupRepo } from './components/RepoPicker';
import SyncBanner from './components/SyncBanner';
import CommandPalette from './components/CommandPalette';
import { buildCommands, type PaletteCommand } from './components/commandRegistry';
import {
  fetchContainerStats,
  fetchRepoDiff,
  fetchRepos,
  fetchSystemHealth,
  flushOutbox,
  startJobRun,
  subscribeApiFailures,
  warmRepoMetrics,
} from './apiClient';
import type { ContainerStatsRow, SystemHealthResponse } from './apiClient';
import type { RepoInfo } from './reviewTypes';
import { loadContainerStats, loadRepoDiff, loadRepos, loadSystemHealth, useMock } from './views/mockViews';
import RepoDiffModal from './views/RepoDiffModal';
import type { RepoDiffData } from './views/RepoDiffModal';
import ContainerDrawer from './views/ContainerDrawer';
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

// Persistent console nav (#896): one bar, six destinations. Review
// and Jobs live inside Now as sections (not routes); the nav reaches
// them through the palette's section-request path.
export type NavEntry =
  | { kind: 'tab'; id: Tab; label: string; key: string }
  | { kind: 'section'; id: 'review' | 'jobs'; label: string };

export const NAV_ENTRIES: NavEntry[] = [
  { kind: 'tab', id: 'now', label: 'Now', key: '1' },
  { kind: 'tab', id: 'chat', label: 'Chat', key: '2' },
  { kind: 'tab', id: 'briefing', label: 'Briefing', key: '3' },
  { kind: 'tab', id: 'history', label: 'History', key: '4' },
  { kind: 'section', id: 'review', label: 'Review' },
  { kind: 'section', id: 'jobs', label: 'Jobs' },
];

export type NowSection = 'review' | 'jobs' | 'queue' | null;

// Pressed rule for the unified nav: tabs light on tab match, except
// Now yields to an open Review/Jobs section; Now stays lit for the
// inbox and for sections without an entry (Queue).
export function navPressed(tab: Tab, section: NowSection, entry: NavEntry): boolean {
  if (entry.kind === 'tab') {
    if (tab !== entry.id) return false;
    if (entry.id !== 'now') return true;
    return section !== 'review' && section !== 'jobs';
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
  const [statsSeq, setStatsSeq] = useState(0);
  const [restartRequested, setRestartRequested] = useState<string | null>(null);
  const [restartError, setRestartError] = useState<string | null>(null);
  // Command palette (issue #810): open flag plus one nonce per
  // cross-view request, so a command lands even when its view is
  // already mounted (or mounts fresh on the tab switch).
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [sectionRequest, setSectionRequest] = useState<{
    section: 'review' | 'jobs' | null;
    nonce: number;
  } | null>(null);
  // Open Now section mirrored up from NowView (#896): drives the
  // nav pressed state for the Review/Jobs entries.
  const [nowSection, setNowSection] = useState<NowSection>(null);
  const [reportRequest, setReportRequest] = useState(0);
  const [chatFocusRequest, setChatFocusRequest] = useState(0);
  const [briefingReloadRequest, setBriefingReloadRequest] = useState(0);
  const [createRequest, setCreateRequest] = useState(0);

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
        openStats: () => setStatsOpen(true),
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

  // Diff modal fetch: on open only; the modal is a point-in-time
  // read (close + reopen retries).
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
          diffStat: d.diff_stat,
          diff: d.diff,
          truncated: d.truncated,
        });
      } catch (err) {
        if (!cancelled) setDiffError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [diffRepo]);

  // Stats drawer fetch: on open + manual refresh (statsSeq), never
  // a ticker — point-in-time CPU/RAM per the issue.
  useEffect(() => {
    if (!statsOpen) return;
    let cancelled = false;
    setStatsLoading(true);
    setStatsError(null);
    (async () => {
      try {
        const s = useMock() ? loadContainerStats() : await fetchContainerStats();
        if (cancelled) return;
        setStatsRows(s.containers);
      } catch (err) {
        if (!cancelled) setStatsError(err instanceof Error ? err.message : String(err));
      } finally {
        if (!cancelled) setStatsLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statsOpen, statsSeq]);

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

  function closeStats() {
    setStatsOpen(false);
    setRestartRequested(null);
    setRestartError(null);
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
          onClick={() => setStatsOpen(true)}
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
          onClose={() => setDiffRepo(null)}
        />
      )}
      {statsOpen && (
        <ContainerDrawer
          rows={statsRows}
          loading={statsLoading}
          error={statsError}
          restartRequested={restartRequested}
          restartError={restartError}
          onRestart={(action) => void onRestartContainer(action)}
          onRefresh={() => setStatsSeq((n) => n + 1)}
          onClose={closeStats}
        />
      )}
      <nav className="topnav" aria-label="Views">
        {NAV_ENTRIES.map((t) => (
          <button
            key={t.id}
            type="button"
            aria-pressed={navPressed(tab, nowSection, t)}
            aria-keyshortcuts={t.kind === 'tab' ? t.key : undefined}
            title={t.kind === 'tab' ? `${t.label} (${t.key})` : t.label}
            onClick={() => goNav(t)}
          >
            {t.label}
          </button>
        ))}
      </nav>
      <ShortcutHint />
      {tab === 'now' && (
        <NowView
          repo={repo}
          defaultBranch={repos.find((r) => r.name === repo)?.branch}
          health={health}
          reportContext={`${isRollupRepo(repo) ? 'All repos' : `Repo ${repo}`} · ${healthSummary(health)}`}
          onAskAgent={askAgent}
          onOpenTab={(t) => setTab(t)}
          sectionRequest={sectionRequest}
          reportRequest={reportRequest}
          createRequest={createRequest}
          onSectionChange={setNowSection}
          onOpenStats={() => setStatsOpen(true)}
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
            health={health}
            onOpenStats={() => setStatsOpen(true)}
            active={tab === 'history'}
          />
        </div>
      )}
      <ShortcutCheatsheet open={helpOpen} onClose={() => setHelpOpen(false)} />
    </main>
  );
}
