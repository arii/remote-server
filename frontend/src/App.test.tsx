import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import App from './App';
import { cleanBriefingLine } from './views/BriefingView';
import { ApprovalCard, cleanAgentText, traceLine } from './views/ChatView';
import { loadHistory, loadWorkflowHealth } from './views/mockViews';
import { plainLanguage, toHistoryRows } from './views/historyUtils';
import { resultLine } from './views/JobsView';

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

  it('collapses consecutive job lifecycle chatter into one activity row', () => {
    const rows = toHistoryRows(
      [],
      [
        { kind: 'job', event: 'step auto-done', ts: 3 },
        { kind: 'job', event: 'created', ts: 2 },
        { kind: 'job', event: 'step done', ts: 1 },
      ],
    );
    expect(rows).toHaveLength(1);
    expect(rows[0].summary).toMatch(/Job activity ×3/);
    expect(rows[0].recovery).toBe('');
  });

  it('prefers backend error reasons over generic failure text', () => {
    const rows = toHistoryRows(
      [],
      [
        {
          kind: 'chat',
          event: 'chat failed',
          error: 'agent runtime misconfigured (exit 2)',
          ts: 1,
        },
      ],
    );
    expect(rows).toHaveLength(1);
    expect(rows[0].summary).toContain('misconfigured');
    expect(rows[0].recovery).toBeTruthy();
  });

  it('keeps mixed-kind routine chatter in separate rows', () => {
    const rows = toHistoryRows(
      [],
      [
        { kind: 'chat', event: 'chat started', ts: 2 },
        { kind: 'job', event: 'created', ts: 1 },
      ],
    );
    expect(rows).toHaveLength(2);
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

describe('briefing sanitation', () => {
  it('strips reviewer HTML comments and caps length', () => {
    const line =
      "- #243: @boomtick[bot] APPROVED — '<!-- ai-review-count: 1 --> <!-- x -->'" +
      ` ${'detail '.repeat(60)}`;
    const clean = cleanBriefingLine(line);
    expect(clean).not.toContain('<!--');
    expect(clean).toContain('APPROVED');
    expect(clean.length).toBeLessThanOrEqual(280);
  });

  it('strips unclosed trailing comments and review-state blobs', () => {
    const clean = cleanBriefingLine(
      "- #243: APPROVED '<!-- ai-review-state: eyJmaW5kaW5ncyI6W10",
    );
    expect(clean).not.toContain('<!--');
    expect(clean).not.toContain('eyJmaW5kaW5ncyI6W10');
    expect(clean).toContain('APPROVED');
  });
});

describe('chat agent text', () => {
  it('strips INTERRUPT blobs and trace paths from replies', () => {
    const text = cleanAgentText(
      'Looking into it…\nINTERRUPT: {"thread_id":"x"}\ntrace: /tmp/t.jsonl',
    );
    expect(text).toBe('Looking into it…');
  });

  it('renders trace events as terse timeline lines', () => {
    expect(
      traceLine({ ts: 100, type: 'run_start', script: 's.py', thread: 't', model: 'm' }, 100),
    ).toContain('START s.py');
    expect(traceLine({ ts: 101.5, type: 'model_request', n_messages: 3 }, 100)).toContain(
      'LLM req n_msgs=3',
    );
    expect(
      traceLine({ ts: 102, type: 'tool_call', name: 'g', duration_s: 0.4, ok: true }, 100),
    ).toMatch(/ok g/);
    expect(
      traceLine({ ts: 103, type: 'tool_call', name: 'p', duration_s: 0.8, ok: false, error: 'nope' }, 100),
    ).toMatch(/ERR p/);
    expect(traceLine({ ts: 104, type: 'run_end', duration_s: 2, ok: true }, 100)).toContain(
      'END ok=true',
    );
    expect(traceLine({ ts: 105, type: 'future_thing' }, 100)).toContain('? future_thing');
  });

  it('renders the approval card with proposals and decisions', () => {
    const html = renderToStaticMarkup(
      <ApprovalCard
        approval={{
          session_id: 's',
          action_requests: [{ args: { kind: 'td', command: 'td hi' } }],
          proposals: ['PROPOSE td\nCOMMAND: td hi\nEND PROPOSE'],
        }}
        decided={null}
        onDecide={() => {}}
      />,
    );
    expect(html).toContain('Approval needed');
    expect(html).toContain('Run: td hi');
    expect(html).toContain('COMMAND: td hi');
    expect(html).toContain('Approve');
    expect(html).toContain('Reject');
    const decided = renderToStaticMarkup(
      <ApprovalCard approval={{ session_id: 's' }} decided="Approved." onDecide={() => {}} />,
    );
    expect(decided).toContain('Approved.');
    expect(decided).not.toContain('Reject');
  });
});

describe('job result line', () => {
  it('surfaces the last meaningful line past command echoes', () => {
    expect(resultLine('$ docker compose exec -T terminal bash\nwrote .agents/plans/x.md\n')).toBe(
      'wrote .agents/plans/x.md',
    );
  });
  it('returns empty when only echoes or blank output exist', () => {
    expect(resultLine('$ echo hi\n\n')).toBe('');
    expect(resultLine('')).toBe('');
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
