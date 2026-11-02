import { useEffect, useState } from 'react';
import SessionList from './components/SessionList';
import SessionDetail from './components/SessionDetail';
import SystemHealth from './components/SystemHealth';
import BriefingView from './views/BriefingView';
import JobsView from './views/JobsView';
import LedgerView from './views/LedgerView';
import ActionsView from './views/ActionsView';
import {
  fetchSessionDetail,
  fetchSessionList,
  fetchSystemHealth,
} from './apiClient';
import type { DetailResponse, HealthStatus, ListResponse } from './types';

type Tab = 'sessions' | 'briefing' | 'jobs' | 'ledger' | 'actions';

const TABS: Tab[] = ['sessions', 'briefing', 'jobs', 'ledger', 'actions'];

export default function App() {
  const [tab, setTab] = useState<Tab>('sessions');
  const [selected, setSelected] = useState<string | null>(null);
  const [list, setList] = useState<ListResponse | null>(null);
  const [detail, setDetail] = useState<DetailResponse | { error: string } | null>(null);
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [sessions, sys] = await Promise.all([
          fetchSessionList(),
          fetchSystemHealth(),
        ]);
        if (cancelled) return;
        setList(sessions);
        setHealth({
          ok: true,
          sessions: sessions.sessions.length,
          timestamp: Date.now(),
        });
        void sys;
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const d = await fetchSessionDetail(selected);
        if (!cancelled) setDetail(d);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selected]);

  if (error) {
    return (
      <main className="layout">
        <div data-testid="app-error" role="alert">
          Backend unavailable: {error}
        </div>
      </main>
    );
  }

  if (!list || !health) {
    return (
      <main className="layout">
        <div data-testid="app-loading">Loading sessions…</div>
      </main>
    );
  }

  return (
    <main className="layout">
      <header className="page-head">
        <div>
          <h1>Sessions</h1>
          <p className="row-meta">Browse recorded Pi sessions (live backend).</p>
        </div>
        <SystemHealth health={health} />
      </header>
      <nav className="actions" aria-label="Views">
        {TABS.map((t) => (
          <button
            key={t}
            type="button"
            aria-pressed={tab === t}
            onClick={() => setTab(t)}
          >
            {t}
          </button>
        ))}
      </nav>
      {tab === 'sessions' && (
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
      )}
      {tab === 'briefing' && <BriefingView repo="remote-server" />}
      {tab === 'jobs' && <JobsView />}
      {tab === 'ledger' && <LedgerView />}
      {tab === 'actions' && (
        <ActionsView
          onAction={(a) => {
            if (a === 'Clear selection') setSelected(null);
          }}
        />
      )}
    </main>
  );
}
