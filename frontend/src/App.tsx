import { useState } from 'react';
import NowView from './views/NowView';
import HistoryView from './views/HistoryView';

type Tab = 'now' | 'history';

const TABS: Tab[] = ['now', 'history'];
const ACTIVE_REPO = 'remote-server';

export default function App() {
  const [tab, setTab] = useState<Tab>('now');

  return (
    <main className="layout">
      <header className="page-head">
        <div>
          <h1>Now</h1>
          <p className="row-meta">What needs your attention.</p>
        </div>
        <span
          data-testid="repo-chip"
          className="repo-chip"
          aria-label={`Active repo: ${ACTIVE_REPO}`}
        >
          {ACTIVE_REPO}
        </span>
      </header>
      <nav className="actions" aria-label="Views">
        {TABS.map((t) => (
          <button
            key={t}
            type="button"
            aria-pressed={tab === t}
            onClick={() => setTab(t)}
          >
            {t === 'now' ? 'Now' : 'History'}
          </button>
        ))}
      </nav>
      {tab === 'now' ? <NowView repo={ACTIVE_REPO} /> : <HistoryView repo={ACTIVE_REPO} />}
    </main>
  );
}
