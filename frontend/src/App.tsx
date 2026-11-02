import { useEffect, useState } from 'react';
import NowView from './views/NowView';
import ReviewView from './views/ReviewView';
import HistoryView from './views/HistoryView';
import RepoPicker from './components/RepoPicker';
import { fetchRepos } from './apiClient';
import type { RepoInfo } from './reviewTypes';
import { loadRepos, useMock } from './views/mockViews';

// Nav is intentionally extensible: Jobs/Chat land as separate views later
// by adding one entry here plus a branch in the body below.
type Tab = 'now' | 'review' | 'history';

const TABS: Array<{ id: Tab; label: string }> = [
  { id: 'now', label: 'Now' },
  { id: 'review', label: 'Review' },
  { id: 'history', label: 'History' },
];

const FALLBACK_REPOS: RepoInfo[] = [
  { name: 'remote-server', full: 'arii/remote-server', branch: 'main', clean: true },
];

export default function App() {
  const [tab, setTab] = useState<Tab>('now');
  const [repos, setRepos] = useState<RepoInfo[]>(FALLBACK_REPOS);
  const [repo, setRepo] = useState('remote-server');

  useEffect(() => {
    if (useMock()) {
      const r = loadRepos();
      setRepos(r);
      if (!r.some((x) => x.name === repo) && r.length > 0) setRepo(r[0].name);
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
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <main className="layout">
      <header className="page-head">
        <div>
          <h1>Now</h1>
          <p className="row-meta">What needs your attention.</p>
        </div>
        <div className="actions">
          <RepoPicker repos={repos} value={repo} onChange={setRepo} />
          <span
            data-testid="repo-chip"
            className="repo-chip"
            aria-label={`Active repo: ${repo}`}
          >
            {repo}
          </span>
        </div>
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
      {tab === 'now' && <NowView repo={repo} />}
      {tab === 'review' && <ReviewView repo={repo} />}
      {tab === 'history' && <HistoryView repo={repo} />}
    </main>
  );
}
