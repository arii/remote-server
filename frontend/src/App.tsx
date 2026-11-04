import { useEffect, useMemo, useState } from 'react';
import NowView from './views/NowView';
import ChatView from './views/ChatView';
import BriefingView from './views/BriefingView';
import HistoryView from './views/HistoryView';
import RepoPicker, { ALL_REPOS, isRollupRepo } from './components/RepoPicker';
import SyncBanner from './components/SyncBanner';
import CommandPalette, { isPaletteHotkey } from './components/CommandPalette';
import { buildCommands, type PaletteCommand } from './components/commandRegistry';
import {
  fetchContainerStats,
  fetchRepoDiff,
  fetchRepos,
  fetchSystemHealth,
  startJobRun,
  subscribeApiFailures,
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

// Bug-report prefill context (issue #818): the existing Repo/health
// prefix plus the current view, in-app path, and served JS bundle
// basename, so triage stops re-asking which bundle produced the
// report. SSR-safe: render tests have no window/document.
export function bundleBasename(src: string | null | undefined): string | null {
  if (!src) return null;
  const base = src.split('?')[0].split('#')[0].split('/').pop() ?? '';
  return base || null;
}

export function servedBundleName(): string | null {
  try {
    if (typeof document === 'undefined') return null;
    return bundleBasename(document.querySelector('script[src]')?.getAttribute('src'));
  } catch {
    return null;
  }
}

export function reportPath(): string {
  try {
    if (typeof window === 'undefined') return '';
    return `${window.location.pathname}${window.location.search}`;
  } catch {
    return '';
  }
}

export function buildReportContext({
  repo,
  health,
  tab,
  path,
  bundle,
}: {
  repo: string;
  health: SystemHealthResponse | null;
  tab: string;
  path: string;
  bundle: string | null;
}): string {
  const parts = [
    `${isRollupRepo(repo) ? 'All repos' : `Repo ${repo}`} · ${healthSummary(health)}`,
    `view: ${tab}`,
  ];
  if (path) parts.push(path);
  if (bundle) parts.push(`bundle: ${bundle}`);
  return parts.join(' · ');
}

type Tab = 'now' | 'chat' | 'briefing' | 'history';

// Now is the triage inbox (attention + Review/Jobs sections); History
// is the secondary trail. Review/Jobs live inside Now, not as routes.
const TABS: Array<{ id: Tab; label: string; key: string }> = [
  { id: 'now', label: 'Now', key: '1' },
  { id: 'chat', label: 'Chat', key: '2' },
  { id: 'briefing', label: 'Briefing', key: '3' },
];

const SECONDARY_TABS: Array<{ id: Tab; label: string; key: string }> = [
  { id: 'history', label: 'History', key: '4' },
];

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
    section: 'review' | 'jobs';
    nonce: number;
  } | null>(null);
  const [reportRequest, setReportRequest] = useState(0);
  const [chatFocusRequest, setChatFocusRequest] = useState(0);
  const [briefingReloadRequest, setBriefingReloadRequest] = useState(0);

  // Cheatsheet visibility for the app-level shortcuts (#808). Tab/send
  // shortcuts suspend while a modal/drawer owns the screen; the
  // cheatsheet itself stays reachable.
  const [helpOpen, setHelpOpen] = useState(false);
  useAppShortcuts({
    suspended: diffRepo !== null || statsOpen,
    helpOpen,
    onTab: setTab,
    onSend: () => clickChatSend(),
    onToggleHelp: () => setHelpOpen((v) => !v),
    onCloseHelp: () => setHelpOpen(false),
  });

  function askAgent(seed: string) {
    setChatSeed(seed);
    setTab('chat');
  }

  // Ctrl+K / Cmd+K toggles the palette from anywhere. The chord
  // never collides with plain typing, so no editable-focus guard.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (isPaletteHotkey(e)) {
        e.preventDefault();
        setPaletteOpen((o) => !o);
      }
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

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
      cmd.id === 'report-bug'
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
    (async () => {
      try {
        const live = await fetchRepos();
        if (live.repos.length > 0) {
          setRepos(live.repos);
          if (repo !== ALL_REPOS && !live.repos.some((x) => x.name === repo))
            setRepo(live.repos[0].name);
        }
      } catch {
        // Keep fallback repo; header still renders the picker.
      }
      try {
        setHealth(await fetchSystemHealth());
      } catch {
        // Header pill stays in loading state; History shows the error.
      }
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
      <nav className="actions" aria-label="Views">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            aria-pressed={tab === t.id}
            aria-keyshortcuts={t.key}
            title={`${t.label} (${t.key})`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </nav>
      <nav className="actions secondary" aria-label="Secondary views">
        {SECONDARY_TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            aria-pressed={tab === t.id}
            aria-keyshortcuts={t.key}
            title={`${t.label} (${t.key})`}
            onClick={() => setTab(t.id)}
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
          reportContext={buildReportContext({
            repo,
            health,
            tab,
            path: reportPath(),
            bundle: servedBundleName(),
          })}
          onAskAgent={askAgent}
          onOpenTab={(t) => setTab(t)}
          sectionRequest={sectionRequest}
          reportRequest={reportRequest}
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
      {tab === 'briefing' &&
        (isRollupRepo(repo) ? (
          <RollupGate view="Briefing" />
        ) : (
          <BriefingView
            repo={repo}
            onAskAgent={askAgent}
            onOpenHistory={() => setTab('history')}
            reloadRequest={briefingReloadRequest}
          />
        ))}
      {tab === 'history' &&
        (isRollupRepo(repo) ? (
          <RollupGate view="History" />
        ) : (
          <HistoryView repo={repo} health={health} />
        ))}
      <ShortcutCheatsheet open={helpOpen} onClose={() => setHelpOpen(false)} />
    </main>
  );
}
