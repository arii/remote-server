import { useState } from 'react';
import HistoryView from './views/HistoryView';

type Tab = 'now' | 'history';

const TABS: Tab[] = ['now', 'history'];

export default function App() {
  const [tab, setTab] = useState<Tab>('history');

  return (
    <main className="layout">
      <header className="page-head">
        <div>
          <h1>Remote server</h1>
          <p className="row-meta">Live backend.</p>
        </div>
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
      {tab === 'now' && (
        <section data-testid="now-view">
          <h2>Now</h2>
          <p className="row-meta" data-testid="now-placeholder">
            Current status lives here — built separately.
          </p>
        </section>
      )}
      {tab === 'history' && <HistoryView />}
    </main>
  );
}
