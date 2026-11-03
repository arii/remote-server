import { useEffect, useState } from 'react';
import NowView from './views/NowView';
import ChatView from './views/ChatView';
import BriefingView from './views/BriefingView';
import HistoryView from './views/HistoryView';
import RepoPicker from './components/RepoPicker';
import { fetchRepos, fetchSystemHealth } from './apiClient';
import type { SystemHealthResponse } from './apiClient';
import type { RepoInfo } from './reviewTypes';
import { loadRepos, loadSystemHealth, useMock } from './views/mockViews';

// Stack root for Terminal/Code links: relative in production (same host),
// VITE_STACK_URL override for `vite dev` on a port without those routes.
function stackBase(): string {
  return import.meta.env.VITE_STACK_URL ?? '';
}

function healthSummary(health: SystemHealthResponse | null): string {
  if (!health) return 'Status: loading…';
  const up = health.containers.filter((c) => c.state === 'running').length;
  return `Status: ${up}/${health.containers.length} containers up`;
}

type Tab = 'now' | 'chat' | 'briefing' | 'history';

// Now is the triage inbox (attention + Review/Jobs sections); History
// is the secondary trail. Review/Jobs live inside Now, not as routes.
const TABS: Array<{ id: Tab; label: string }> = [
  { id: 'now', label: 'Now' },
  { id: 'chat', label: 'Chat' },
  { id: 'briefing', label: 'Briefing' },
];

const SECONDARY_TABS: Array<{ id: Tab; label: string }> = [
  { id: 'history', label: 'History' },
];

const FALLBACK_REPOS: RepoInfo[] = [
  { name: 'remote-server', full: 'arii/remote-server', branch: 'main', clean: true },
];

export default function App() {
  const [tab, setTab] = useState<Tab>('now');
  const [repos, setRepos] = useState<RepoInfo[]>(FALLBACK_REPOS);
  const [repo, setRepo] = useState('remote-server');
  const [health, setHealth] = useState<SystemHealthResponse | null>(null);
  const [chatSeed, setChatSeed] = useState<string | null>(null);

  function askAgent(seed: string) {
    setChatSeed(seed);
    setTab('chat');
  }

  useEffect(() => {
    if (useMock()) {
      const r = loadRepos();
      setRepos(r);
      if (!r.some((x) => x.name === repo) && r.length > 0) setRepo(r[0].name);
      setHealth(loadSystemHealth());
      return;
    }
    (async () => {
      try {
        const live = await fetchRepos();
        if (live.repos.length > 0) {
          setRepos(live.repos);
          if (!live.repos.some((x) => x.name === repo)) setRepo(live.repos[0].name);
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

  return (
    <main className="layout">
      <header className="toolbar">
        <RepoPicker repos={repos} value={repo} onChange={setRepo} />
        <span
          data-testid="repo-chip"
          className="repo-chip"
          aria-label={`Active repo: ${repo}`}
        >
          {repo}
        </span>
        <span data-testid="stack-status" className="row-meta">
          {healthSummary(health)}
        </span>
        <span className="spacer" />
        <a data-testid="tool-terminal" href={`${stackBase()}/`}>
          Terminal
        </a>
        <a data-testid="tool-code" href={`${stackBase()}/code`}>
          Code
        </a>
      </header>
      <nav className="actions" aria-label="Views">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            aria-pressed={tab === t.id}
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
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </nav>
      {tab === 'now' && (
        <NowView
          repo={repo}
          health={health}
          reportContext={`Repo ${repo} · ${healthSummary(health)}`}
          onAskAgent={askAgent}
        />
      )}
      {tab === 'chat' && (
        <ChatView repo={repo} seed={chatSeed} onSeedUsed={() => setChatSeed(null)} />
      )}
      {tab === 'briefing' && <BriefingView repo={repo} />}
      {tab === 'history' && <HistoryView repo={repo} health={health} />}
    </main>
  );
}
