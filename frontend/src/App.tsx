import { useMemo, useState } from 'react';
import SessionList from './components/SessionList';
import SessionDetail from './components/SessionDetail';
import SystemHealth from './components/SystemHealth';
import { loadHealth, loadSessionDetail, loadSessionList } from './mockLoader';

export default function App() {
  const [selected, setSelected] = useState<string | null>(null);
  const list = useMemo(() => loadSessionList(), []);
  const detail = useMemo(
    () => (selected ? loadSessionDetail(selected) : null),
    [selected],
  );
  const health = useMemo(() => loadHealth(list.sessions.length), [list]);

  return (
    <main className="layout">
      <header className="page-head">
        <div>
          <h1>Sessions</h1>
          <p className="row-meta">Browse recorded Pi sessions (mock data).</p>
        </div>
        <SystemHealth health={health} />
      </header>
      <div className="columns">
        <section aria-label="Session list">
          <SessionList sessions={list.sessions} selected={selected} onSelect={setSelected} />
        </section>
        <section aria-label="Session detail">
          <SessionDetail detail={detail} />
          {selected && (
            <div className="actions" role="group" aria-label="Session actions">
              <button type="button" onClick={() => setSelected(null)}>
                Clear selection
              </button>
            </div>
          )}
        </section>
      </div>
    </main>
  );
}
