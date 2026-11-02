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

  it('renders ledger ts timestamps and gates recovery to failures', () => {
    const rows = toHistoryRows(
      [],
      [
        { kind: 'chat', event: 'chat finished', ts: 1793652000 },
        { kind: 'webhook', event: 'Push was rejected — the remote has new commits', ts: 1793652100, ok: false },
      ],
    );
    expect(rows[0].summary).toContain('rejected');
    expect(rows[0].at).not.toContain('unknown time');
    expect(rows[0].recovery).toBeTruthy();
    expect(rows[1].recovery).toBe('');
  });

  it('collapses consecutive chat lifecycle chatter into one activity row', () => {
    const rows = toHistoryRows(
      [],
      [
        { kind: 'chat', event: 'chat started', ts: 3 },
        { kind: 'chat', event: 'chat finished', ts: 2 },
        { kind: 'chat', event: 'chat aborted', ts: 1 },
      ],
    );
    expect(rows).toHaveLength(1);
    expect(rows[0].summary).toMatch(/Chat session activity ×3/);
    expect(rows[0].recovery).toBe('');
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
