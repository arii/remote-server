import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import App from './App';
import { loadHistory, loadWorkflowHealth } from './views/mockViews';
import { plainLanguage, toHistoryRows } from './views/historyUtils';

describe('history utils', () => {
  it('uses plain language, never raw stderr', () => {
    expect(plainLanguage('FAILED stderr: Traceback blah', 'webhook')).not.toContain('stderr');
    expect(plainLanguage('non-fast-forward rejected', 'webhook')).toContain('remote has new commits');
  });

  it('collapses repeat failures into one incident row with recovery', () => {
    const h = loadHistory();
    const rows = toHistoryRows(h.jobs, h.entries);
    const incident = rows.find((r) => r.repeat > 1);
    expect(incident).toBeDefined();
    expect(incident!.summary).toMatch(/×\d+/);
    expect(incident!.recovery).toBeTruthy();
  });

  it('sorts newest first using real job timestamps', () => {
    const rows = toHistoryRows(
      [
        { id: 'old', kind: 'job', at: 1000 },
        { id: 'new', kind: 'job', at: 2000 },
      ],
      [],
    );
    expect(rows[0].summary).toContain('new');
    expect(rows[1].summary).toContain('old');
  });
});

describe('history fixtures (?mock=1)', () => {
  it('history fixture includes the incident-collapse case', () => {
    const h = loadHistory();
    expect(h.entries.length).toBeGreaterThanOrEqual(3);
  });

  it('workflow-health fixture carries run traces', () => {
    const w = loadWorkflowHealth();
    expect(w.workflows.length).toBeGreaterThanOrEqual(1);
    expect(w.workflows[0].diagnosed_cause).toBeTruthy();
  });
});

describe('app', () => {
  it('App renders Now nav, repo chip, and Now loading state', () => {
    const html = renderToStaticMarkup(<App />);
    expect(html).toContain('data-testid="now-loading"');
    expect(html).toContain('data-testid="repo-chip"');
    expect(html).toContain('History');
  });

  it('App renders Now/History nav only', () => {
    const html = renderToStaticMarkup(<App />);
    expect(html).toContain('Now');
    expect(html).toContain('History');
    expect(html).not.toContain('sessions');
    expect(html).not.toContain('briefing');
  });
});
