import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import SessionList from './components/SessionList';
import SessionDetail from './components/SessionDetail';
import SystemHealth from './components/SystemHealth';
import App from './App';
import { loadHealth, loadSessionDetail, loadSessionList } from './mockLoader';

describe('mock loader (exact fixture shapes)', () => {
  it('loads 3 sessions with pi-file, background, ledger rows', () => {
    const list = loadSessionList();
    expect(list.sessions).toHaveLength(3);
    expect(list.sessions[0]).toMatchObject({ file: '20240101_abcdef.jsonl' });
    expect(list.sessions[1]).toMatchObject({ source: 'background', done: true });
    expect(list.sessions[2]).toMatchObject({ source: 'ledger', kind: 'ledger' });
  });

  it('loads empty + error shapes', () => {
    expect(loadSessionList(true)).toEqual({ sessions: [] });
    expect(loadSessionDetail('missing.jsonl')).toHaveProperty('error');
  });

  it('loads detail turns for the background session', () => {
    const d = loadSessionDetail('bg-1234567890ab.jsonl');
    expect('turns' in d && d.turns).toHaveLength(2);
  });
});

describe('components', () => {
  it('SessionList renders rows and empty state', () => {
    const list = loadSessionList();
    const html = renderToStaticMarkup(
      <SessionList sessions={list.sessions} selected={null} onSelect={() => {}} />,
    );
    expect(html).toContain('Fix login bug');
    expect(html).toContain('data-testid="session-list"');
    const empty = renderToStaticMarkup(
      <SessionList sessions={[]} selected={null} onSelect={() => {}} />,
    );
    expect(empty).toContain('data-testid="session-empty"');
  });

  it('SessionDetail renders placeholder, turns, and error', () => {
    expect(
      renderToStaticMarkup(<SessionDetail detail={null} />),
    ).toContain('data-testid="detail-placeholder"');
    const detail = loadSessionDetail('bg-1234567890ab.jsonl');
    const html = renderToStaticMarkup(<SessionDetail detail={detail} />);
    expect(html).toContain('How does server work?');
    expect(html).toContain('It serves HTTP requests.');
    const err = renderToStaticMarkup(
      <SessionDetail detail={{ error: 'No session matching' }} />,
    );
    expect(err).toContain('data-testid="detail-error"');
  });

  it('App renders Now nav, repo chip, and Now loading state', () => {
    const html = renderToStaticMarkup(<App />);
    expect(html).toContain('data-testid="now-loading"');
    expect(html).toContain('data-testid="repo-chip"');
    expect(html).toContain('History');
  });

  it('SystemHealth reflects session count', () => {
    const html = renderToStaticMarkup(
      <SystemHealth health={loadHealth(3)} />,
    );
    expect(html).toContain('3 sessions');
    expect(html).toContain('Healthy');
  });
});
