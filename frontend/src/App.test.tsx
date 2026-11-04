import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import App from './App';
import AgentDrawer, { pumpTurn } from './views/AgentDrawer';
import InboxStrips from './views/InboxStrips';
import type { AgentRun, InboxFeed, SystemHealthResponse } from './apiClient';
import HoldToConfirm from './components/HoldToConfirm';
import TriageHint from './components/TriageHint';
import RepoPicker, { repoOptionLabel } from './components/RepoPicker';
import BriefingView, {
  BriefingBody,
  briefingActions,
  briefingAllClear,
  briefingFreshness,
  briefingSummary,
  cleanBriefingLine,
  dayTrend,
  truncateChip,
} from './views/BriefingView';
import RepoLabel, { repoLabelText } from './components/RepoLabel';
import ChatView, {
  ApprovalCard,
  CHAT_BACKEND_OPTIONS,
  RunsPane,
  TraceTimeline,
  TypingIndicator,
  awaitingFirstToken,
  cleanAgentText,
  hintLine,
  isKeyboardSend,
  runButtonLabel,
  traceLine,
  traceSummary,
} from './views/ChatView';
import HistoryView from './views/HistoryView';
import JobsView from './views/JobsView';
import {
  attachIdFromConflict,
  jobsListActive,
  planExecuteSeed,
  planNameFromResult,
  repairCandidates,
  resultLine,
} from './views/jobsUtils';
import NowView, { ReportDialog, StackSection, nowPollMs } from './views/NowView';
import ReviewView, { PrDetails, PrRowMerge, detailsPromptVisible } from './views/ReviewView';
import { loadBriefing, loadHistory, loadPrDetails, loadWorkflowHealth } from './views/mockViews';
import { jobPassesKindFilter, plainLanguage, tailText, toHistoryRows } from './views/historyUtils';
import { detailsMergeBlockReason, mergeBlockReason } from './reviewTypes';
import type { PrDetailsResponse, PrRow } from './reviewTypes';

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

  it('marks failures bad, routine chatter muted, rest ok', () => {
    const rows = toHistoryRows(
      [],
      [
        { kind: 'webhook', event: 'fetch failed', ts: 3, ok: false },
        { kind: 'chat', event: 'chat started', ts: 2 },
        { kind: 'system', event: 'buttons started', ts: 1 },
      ],
    );
    expect(rows.map((r) => r.tone)).toEqual(['bad', 'muted', 'ok']);
  });

  it('repeats claim failure only for actual failures', () => {
    const rows = toHistoryRows(
      [],
      [
        { kind: 'system', event: 'buttons started', ts: 2 },
        { kind: 'system', event: 'buttons started', ts: 1 },
      ],
    );
    expect(rows[0].summary).not.toContain('failure');
  });

  it('filters job rows by kind, all jobs answer to run', () => {
    expect(jobPassesKindFilter('create-plan', 'all')).toBe(true);
    expect(jobPassesKindFilter('create-plan', 'chat')).toBe(false);
    expect(jobPassesKindFilter('create-plan', 'run')).toBe(true);
    expect(jobPassesKindFilter(undefined, 'chat')).toBe(false);
  });

  it('carries expandable detail beyond the summary', () => {
    const rows = toHistoryRows(
      [],
      [
        {
          kind: 'chat',
          event: 'chat finished',
          repo: 'remote-server',
          session_id: 'abc123',
          exit: 0,
          elapsed: 34.6,
          ts: 1,
        },
      ],
    );
    expect(rows[0].detail).toContain('event: chat finished');
    expect(rows[0].detail).toContain('session: abc123');
    expect(rows[0].detail).toContain('elapsed: 34.6s');
  });

  it('keeps every collapsed group member with its own full fields', () => {
    const rows = toHistoryRows(
      [],
      [
        { kind: 'webhook', event: 'webhook pull failed: fetch first (non-fast-forward)', repo: 'r', ts: 3 },
        { kind: 'webhook', event: 'webhook pull failed: fetch first (non-fast-forward)', repo: 'r', ts: 2 },
        { kind: 'webhook', event: 'webhook pull failed: fetch first (non-fast-forward)', repo: 'r', ts: 1 },
      ],
    );
    expect(rows).toHaveLength(1);
    expect(rows[0].repeat).toBe(3);
    expect(rows[0].members).toHaveLength(3);
    for (const m of rows[0].members) {
      expect(m.detail).toContain('event: webhook pull failed');
      expect(m.at).not.toContain('unknown time');
    }
  });

  it('carries chat prompts and run command/elapsed into member details', () => {
    const rows = toHistoryRows(
      [],
      [
        { kind: 'run', action: 'td', command: 'td audit-pr 12', exit: 0, elapsed_s: 12.5, repo: 'r', ts: 2 },
        { kind: 'chat', event: 'chat started', session_id: 's1', prompt: 'why is CI red?', repo: 'r', ts: 1 },
      ],
    );
    expect(rows).toHaveLength(2);
    expect(rows[0].members[0].detail).toContain('command: td audit-pr 12');
    expect(rows[0].members[0].detail).toContain('elapsed: 12.5s');
    expect(rows[1].members[0].detail).toContain('prompt: why is CI red?');
    expect(rows[1].members[0].detail).toContain('session: s1');
  });

  it('tags job members with their run id for output fetch', () => {
    const rows = toHistoryRows([{ id: 'abc123def456', kind: 'briefing', at: 1000 }], []);
    expect(rows).toHaveLength(1);
    expect(rows[0].members[0].runId).toBe('abc123def456');
  });
});

describe('tailText', () => {
  it('returns short output unchanged', () => {
    expect(tailText('a\nb\nc')).toBe('a\nb\nc');
    expect(tailText('')).toBe('');
  });

  it('keeps the last N lines and reports hidden count', () => {
    const out = Array.from({ length: 10 }, (_, i) => `line ${i + 1}`).join('\n');
    const tailed = tailText(out, 3);
    expect(tailed).toContain('line 8\nline 9\nline 10');
    expect(tailed).toContain('7 earlier lines hidden');
    expect(tailed).not.toContain('line 7\n');
  });

  it('uses the singular for one hidden line', () => {
    expect(tailText('a\nb', 1)).toContain('1 earlier line hidden');
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

  it('strips leading markdown list markers', () => {
    expect(cleanBriefingLine('- PR #416 overlaps with #418 on: docs/x.md')).toBe(
      'PR #416 overlaps with #418 on: docs/x.md',
    );
    expect(cleanBriefingLine('* starred item')).toBe('starred item');
    expect(cleanBriefingLine('  - indented dash')).toBe('indented dash');
    expect(cleanBriefingLine('no marker here')).toBe('no marker here');
  });
});

describe('briefing all-clear', () => {
  const empty = {
    repo: 'r',
    attention: [],
    prs: [],
    prs_err: '',
    reviews_lines: [],
    board: [],
    board_lines: [],
    board_err: '',
    conflicts_lines: [],
    overlaps_lines: [],
    failing_runs: [],
    workflow_lines: [],
    runs_err: '',
  };
  it('condenses a fully empty briefing to one all-clear', () => {
    expect(briefingAllClear(empty)).toBe(true);
  });
  it('keeps sections when anything has content or errored', () => {
    expect(briefingAllClear({ ...empty, prs: [{ number: 1 }] })).toBe(false);
    expect(briefingAllClear({ ...empty, attention: ['x'] })).toBe(false);
    expect(briefingAllClear({ ...empty, conflicts_lines: ['- PR #1'] })).toBe(false);
    expect(briefingAllClear({ ...empty, board: [{ branch: 'b' }] })).toBe(false);
    expect(briefingAllClear({ ...empty, runs_err: 'boom' })).toBe(false);
  });
});

describe('briefing substance', () => {
  const empty = {
    repo: 'r',
    attention: [],
    prs: [],
    prs_err: '',
    reviews_lines: [],
    board: [],
    board_lines: [],
    board_err: '',
    conflicts_lines: [],
    overlaps_lines: [],
    failing_runs: [],
    workflow_lines: [],
    runs_err: '',
  };
  const noop = () => {};
  it('summarizes counts in one line', () => {
    expect(briefingSummary(empty)).toContain('All clear');
    const busy = {
      ...empty,
      prs: [{ number: 1 }, { number: 2 }],
      conflicts_lines: ['- PR #1'],
      workflow_lines: ['x failed'],
    };
    const line = briefingSummary(busy);
    expect(line).toContain('2 open PRs');
    expect(line).toContain('1 conflict');
    expect(line).toContain('1 failing run');
  });
  it('suggests next actions from state, capped at three', () => {
    const clear = briefingActions(empty);
    expect(clear.length).toBeGreaterThan(0);
    expect(clear[0].seed.length).toBeGreaterThan(0);
    const failing = briefingActions({ ...empty, workflow_lines: ['x failed'] });
    expect(failing.some((a) => a.label.includes('fail'))).toBe(true);
    expect(briefingActions(loadBriefing()).length).toBeLessThanOrEqual(3);
  });
  it('counts last-day events and failures', () => {
    const now = 1793745800;
    expect(
      dayTrend(
        [
          { kind: 'chat', event: 'a', ts: now - 100, ok: true },
          { kind: 'webhook', event: 'b', ts: now - 200, ok: false },
          { kind: 'webhook', event: 'old', ts: now - 100000, ok: false },
        ],
        now,
      ),
    ).toBe('Last 24h: 2 events, 1 failure.');
  });
  it('all-clear body shows actions and a History pointer', () => {
    const html = renderToStaticMarkup(
      <BriefingBody data={empty} ledger={[]} onAskAgent={noop} onOpenHistory={noop} />,
    );
    expect(html).toContain('data-testid="briefing-actions"');
    expect(html).toContain('data-testid="briefing-history"');
  });
  it('non-empty body shows condensed highlights', () => {
    const html = renderToStaticMarkup(
      <BriefingBody
        data={{ ...empty, prs: [{ number: 1, title: 't' }] }}
        ledger={[{ kind: 'chat', event: 'merged #1', ts: 1793745700, ok: true }]}
        onAskAgent={noop}
        onOpenHistory={noop}
      />,
    );
    expect(html).toContain('data-testid="briefing-highlights"');
    expect(html).toContain('data-testid="briefing-summary"');
  });
  it('freshness line stays hidden for live payloads', () => {
    expect(briefingFreshness(empty, 1793745800000)).toBe(null);
    const html = renderToStaticMarkup(
      <BriefingBody data={empty} ledger={[]} onAskAgent={noop} onOpenHistory={noop} />,
    );
    expect(html).not.toContain('data-testid="briefing-freshness"');
  });
  it('freshness line shows age and refresh state for cached payloads', () => {
    const now = 1793745800000;
    expect(briefingFreshness({ ...empty, cached: true, generated_at: now / 1000 - 45 }, now)).toBe(
      'Updated 45s ago',
    );
    expect(
      briefingFreshness({ ...empty, cached: true, stale: true, generated_at: now / 1000 - 90 }, now),
    ).toBe('Updated 1m ago · refreshing…');
    const html = renderToStaticMarkup(
      <BriefingBody
        data={{ ...empty, cached: true, stale: true, generated_at: now / 1000 - 90 }}
        ledger={[]}
        onAskAgent={noop}
        onOpenHistory={noop}
      />,
    );
    expect(html).toContain('data-testid="briefing-freshness"');
    expect(html).toContain('refreshing…');
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

  it('summarizes tool calls with action, duration, and status', () => {
    expect(
      traceSummary({ ts: 102, type: 'tool_call', name: 'grep', duration_s: 0.4, ok: true }, 100),
    ).toEqual({ action: 'grep', timing: '0.40s', status: 'ok' });
    expect(
      traceSummary(
        { ts: 103, type: 'tool_call', name: 'patch', duration_s: 0.8, ok: false, error: 'nope' },
        100,
      ),
    ).toEqual({ action: 'patch', timing: '0.80s', status: 'failed' });
  });

  it('summarizes run boundaries and model events from existing fields', () => {
    expect(
      traceSummary({ ts: 100, type: 'run_start', script: 's.py', thread: 't', model: 'm' }, 100),
    ).toEqual({ action: 'START s.py', timing: '+0.0s', status: 'info' });
    expect(traceSummary({ ts: 101, type: 'model_request', n_messages: 3 }, 100)).toEqual({
      action: 'LLM req',
      timing: '+1.0s',
      status: 'info',
    });
    expect(traceSummary({ ts: 104, type: 'run_end', duration_s: 2, ok: true }, 100)).toEqual({
      action: 'END',
      timing: '2.00s',
      status: 'ok',
    });
  });

  it('falls back to offsets and neutral status when fields are missing', () => {
    expect(traceSummary({ ts: 105, type: 'future_thing' }, 100)).toEqual({
      action: '? future_thing',
      timing: '+5.0s',
      status: 'info',
    });
    expect(traceSummary({ ts: 106, type: 'tool_call' }, 100)).toEqual({
      action: '?',
      timing: '+6.0s',
      status: 'info',
    });
  });

  it('folds trace events on narrow viewports and keeps the full log on desktop', () => {
    const trace = [
      { ts: 100, type: 'run_start', script: 's.py', thread: 't', model: 'm' },
      { ts: 101, type: 'tool_call', name: 'grep', duration_s: 0.4, ok: true },
    ];
    const narrow = renderToStaticMarkup(<TraceTimeline trace={trace} t0={100} narrow />);
    expect(narrow).toContain('data-testid="chat-trace-fold"');
    expect(narrow).toContain('START s.py');
    expect(narrow).toContain('grep');
    expect(narrow).toContain('0.40s');
    expect(narrow).toContain('<details>');
    expect(narrow).not.toContain('open');
    const wide = renderToStaticMarkup(<TraceTimeline trace={trace} t0={100} narrow={false} />);
    expect(wide).not.toContain('<details>');
    expect(wide).toContain('START s.py');
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

describe('chat typing indicator', () => {
  it('shows only while streaming with an empty assistant placeholder', () => {
    expect(awaitingFirstToken(false, [{ role: 'assistant', text: '' }])).toBe(false);
    expect(awaitingFirstToken(true, [])).toBe(false);
    expect(awaitingFirstToken(true, [{ role: 'user', text: 'hi' }])).toBe(false);
    expect(
      awaitingFirstToken(true, [
        { role: 'user', text: 'hi' },
        { role: 'assistant', text: '' },
      ]),
    ).toBe(true);
    expect(
      awaitingFirstToken(true, [
        { role: 'user', text: 'hi' },
        { role: 'assistant', text: 'token…' },
      ]),
    ).toBe(false);
  });

  it('renders a polite live region with three dots', () => {
    const html = renderToStaticMarkup(<TypingIndicator />);
    expect(html).toContain('data-testid="chat-typing"');
    expect(html).toContain('aria-live="polite"');
    expect(html).toContain('typing-dots');
  });
});

describe('chat backend picker', () => {
  const noop = () => {};

  it('offers Auto / Local only / Cloud allowed', () => {
    expect(CHAT_BACKEND_OPTIONS.map((o) => o.value)).toEqual(['auto', 'local', 'cloud']);
    expect(CHAT_BACKEND_OPTIONS.map((o) => o.label)).toEqual([
      'Auto',
      'Local only',
      'Cloud allowed',
    ]);
  });

  it('renders the picker with Auto pressed by default', () => {
    const html = renderToStaticMarkup(<ChatView repo="r" seed={null} onSeedUsed={noop} />);
    expect(html).toContain('data-testid="chat-backend-picker"');
    expect(html).toContain('data-testid="chat-backend-auto"');
    expect(html).toContain('data-testid="chat-backend-local"');
    expect(html).toContain('data-testid="chat-backend-cloud"');
    expect(html).toContain('aria-pressed="true"');
  });
});

describe('chat route hint line', () => {
  const noop = () => {};

  it('names a free local backend', () => {
    expect(
      hintLine({
        ok: true,
        provider: 'ollama',
        model: 'qwen3:8b',
        metered: false,
        rate_in_per_1m: null,
        rate_out_per_1m: null,
        error: '',
      }),
    ).toBe('Next reply via ollama:qwen3:8b · free');
  });

  it('shows the rated metered range', () => {
    expect(
      hintLine({
        ok: true,
        provider: 'nebius',
        model: 'zai-org/GLM-5.3-Flash',
        metered: true,
        rate_in_per_1m: 0.15,
        rate_out_per_1m: 0.5,
        error: '',
      }),
    ).toBe('Next reply via nebius:zai-org/GLM-5.3-Flash · metered ~$0.15–$0.5/1M');
  });

  it('stays a bare tag when unrated, short on failure', () => {
    expect(
      hintLine({
        ok: true,
        provider: 'nebius',
        model: 'Some/Future-Model',
        metered: true,
        rate_in_per_1m: null,
        rate_out_per_1m: null,
        error: '',
      }),
    ).toBe('Next reply via nebius:Some/Future-Model · metered');
    expect(
      hintLine({
        ok: false,
        provider: '',
        model: '',
        metered: false,
        rate_in_per_1m: null,
        rate_out_per_1m: null,
        error: 'Ollama unreachable at http://ollama:11434',
      }),
    ).toBe('Next reply unavailable (Ollama unreachable at http://ollama:11434)');
  });

  it('renders the composer line slot', () => {
    const html = renderToStaticMarkup(<ChatView repo="r" seed={null} onSeedUsed={noop} />);
    expect(html).toContain('data-testid="chat-route-hint"');
    expect(html).toContain('Next reply via …');
  });
});

describe('chat composer order', () => {
  const noop = () => {};
  const runs: AgentRun[] = Array.from({ length: 8 }, (_, i) => ({
    id: `run-${i}`,
    script: 'deep_chat.py',
    model: 'ollama:qwen3:8b',
    thread: `t${i}`,
    started_ts: 1793745700,
    duration_s: 9.8,
    ok: true,
    tools: [],
    tokens: 96,
    preview: `run ${i}`,
    has_prompt: true,
    cost_usd: 0,
    metered: false,
  }));

  it('renders Prompt + Send above Past runs with 8 runs', () => {
    const html = renderToStaticMarkup(
      <ChatView repo="r" seed={null} onSeedUsed={noop} initialRuns={runs} />,
    );
    expect(html).toContain('data-testid="chat-runs"');
    expect(html.indexOf('data-testid="chat-send"')).toBeLessThan(
      html.indexOf('data-testid="chat-runs"'),
    );
    expect(html.indexOf('data-testid="chat-prompt"')).toBeLessThan(
      html.indexOf('data-testid="chat-runs"'),
    );
  });

  it('keeps the agent drawer below the composer', () => {
    const html = renderToStaticMarkup(
      <ChatView repo="r" seed={null} onSeedUsed={noop} initialRuns={runs} />,
    );
    expect(html.indexOf('data-testid="chat-send"')).toBeLessThan(
      html.indexOf('data-testid="agent-drawer"'),
    );
  });
});

describe('chat keyboard send', () => {
  const noop = () => {};

  it('sends on Ctrl+Enter and Cmd+Enter, never on plain Enter', () => {
    expect(isKeyboardSend({ key: 'Enter', ctrlKey: true, metaKey: false })).toBe(true);
    expect(isKeyboardSend({ key: 'Enter', ctrlKey: false, metaKey: true })).toBe(true);
    expect(isKeyboardSend({ key: 'Enter', ctrlKey: false, metaKey: false })).toBe(false);
    expect(isKeyboardSend({ key: 'a', ctrlKey: true, metaKey: false })).toBe(false);
  });

  it('advertises the shortcut on Send and anchors the live tail', () => {
    const html = renderToStaticMarkup(<ChatView repo="r" seed={null} onSeedUsed={noop} />);
    expect(html).toContain('title="Send (Ctrl+Enter or Cmd+Enter)"');
    expect(html).toContain('data-testid="chat-bottom"');
  });
});

describe('past-run button names', () => {
  const noop = () => {};
  const run: AgentRun = {
    id: 'run-7',
    script: 'deep_chat.py',
    model: 'ollama:qwen3:8b',
    thread: 't7',
    started_ts: 1793745700,
    duration_s: 156,
    ok: true,
    tools: [],
    tokens: 11089,
    preview: `No TODOs found in the repo. ${'detail '.repeat(40)}`,
    has_prompt: true,
    cost_usd: 0,
    metered: false,
  };

  it('names id, status, duration, tokens, and day', () => {
    expect(runButtonLabel(run)).toBe('Run run-7, ok, 156s, 11k tokens, 2026-11-03');
    expect(
      runButtonLabel({ ...run, ok: false, tokens: 96, started_ts: 0 }),
    ).toBe('Run run-7, failed, 156s, 96 tokens, unknown time');
  });

  it('keeps the Tab-stop name short while the card text stays visible', () => {
    const html = renderToStaticMarkup(
      <RunsPane
        runs={[run]}
        selected={null}
        detail={null}
        onSelect={noop}
        onContinue={noop}
        onDelete={noop}
        onClearFinished={noop}
        notice={null}
        budget={null}
      />,
    );
    const label =
      html.match(/data-testid="chat-run-run-7"[^>]*aria-label="([^"]*)"/)?.[1] ?? '';
    expect(label.length).toBeGreaterThan(0);
    expect(label.length).toBeLessThan(120);
    expect(label).toContain('run-7');
    expect(html).toContain('No TODOs found in the repo.');
    expect(html).toContain('aria-pressed="false"');
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
  it('extracts the plan filename from a wrote line', () => {
    expect(planNameFromResult('wrote .agents/plans/2026-11-03-briefing-plan.md')).toBe(
      '2026-11-03-briefing-plan.md',
    );
    expect(planNameFromResult('Already up to date.')).toBeNull();
    expect(planNameFromResult('')).toBeNull();
  });
});

describe('repair candidates', () => {
  it('skips blocked and scheduled issues, keeps actionable ones', () => {
    const issues = [
      { number: 1, title: 'fix me', labels: [] },
      { number: 102, title: 'future', labels: [{ name: 'scheduled' }] },
      { number: 324, title: 'decide', labels: ['blocked'] },
    ];
    expect(repairCandidates(issues).map((i) => i.number)).toEqual([1]);
  });
});

describe('jobs live gate', () => {
  it('stays live while any run is unfinished', () => {
    expect(jobsListActive([{ id: 'a', done: true }])).toBe(false);
    expect(jobsListActive([])).toBe(false);
    expect(jobsListActive([{ id: 'a', done: true }, { id: 'b', done: false }])).toBe(true);
    expect(jobsListActive([{ id: 'b' }])).toBe(true);
  });
});

describe('now live cadence', () => {
  it('polls fast with items, slow heartbeat when clear', () => {
    expect(nowPollMs(3)).toBe(15000);
    expect(nowPollMs(1)).toBe(15000);
    expect(nowPollMs(0)).toBe(60000);
  });
});

describe('conflict attach', () => {
  it('extracts the active run id from a 409 detail', () => {
    expect(
      attachIdFromConflict(
        'POST api/jobs/run-td failed: 409 {"detail":"a run is already in progress: 799c6006012d"}',
      ),
    ).toBe('799c6006012d');
    expect(attachIdFromConflict('POST api/jobs/run failed: 400 {"Detail":"bad"}')).toBeNull();
    expect(attachIdFromConflict('a run is already in progress: 799c6006012d')).toBeNull();
    expect(attachIdFromConflict('failed: 409 {"Detail":"busy"}')).toBeNull();
  });
});

describe('merge relevance', () => {
  const ready: PrRow = {
    number: 1,
    title: 't',
    author: 'a',
    branch: 'b',
    isDraft: false,
    ciStatus: 'passing',
    ciLabel: 'passing',
    reviewDecision: 'APPROVED',
    mergeable: 'MERGEABLE',
    url: '',
  };
  it('passes a ready PR and names each blocker', () => {
    expect(mergeBlockReason(ready)).toBeNull();
    expect(mergeBlockReason({ ...ready, isDraft: true })).toContain('draft');
    expect(mergeBlockReason({ ...ready, reviewDecision: '' })).toContain('unapproved');
    expect(mergeBlockReason({ ...ready, ciStatus: 'failing' })).toContain('failing');
    expect(mergeBlockReason({ ...ready, mergeable: 'CONFLICTING' })).toContain('CONFLICTING');
  });
  it('mirrors the single-merge server gate', () => {
    const d = { ci: 'passing', merge: 'clean' } as PrDetailsResponse;
    expect(detailsMergeBlockReason(d)).toBeNull();
    expect(detailsMergeBlockReason({ ...d, ci: 'pending' })).toContain('pending');
    expect(detailsMergeBlockReason({ ...d, merge: 'dirty' })).toContain('dirty');
  });
});

describe('app', () => {
  it('App renders Now nav, one repo readout, and Now loading state', () => {
    const html = renderToStaticMarkup(<App />);
    expect(html).toContain('data-testid="now-loading"');
    expect(html).toContain('data-testid="repo-picker"');
    expect(html).toContain('for="repo-picker"');
    expect(html).not.toContain('data-testid="repo-chip"');
    expect(html).not.toContain('Active repo');
    expect(html).toContain('History');
  });

  it('App renders Now/History nav only', () => {
    const html = renderToStaticMarkup(<App />);
    expect(html).toContain('Now');
    expect(html).toContain('History');
    expect(html).not.toContain('sessions');
    expect(html).not.toContain('briefing');
  });

  it('App folds Review/Jobs into Now and demotes History', () => {
    const html = renderToStaticMarkup(<App />);
    expect(html).toContain('aria-label="Views"');
    expect(html).toContain('aria-label="Secondary views"');
    expect(html).not.toContain('>Review</button>');
    expect(html).not.toContain('>Jobs</button>');
  });
});

describe('inbox strips', () => {
  const feed: InboxFeed = {
    repo: 'remote-server',
    updated_at: 1,
    attention: [],
    merge_ready: {
      count: 2,
      top: [
        { number: 12, title: 'Fix it' },
        { number: 13, title: 'Ship it' },
      ],
    },
    runs: {
      active: { id: 'abc', action: 'run-td', started: 1 },
      recent: [{ id: 'def', action: 'logs', exit: 0 }],
    },
  };

  it('renders merge-ready and active-run strips', () => {
    const html = renderToStaticMarkup(
      <InboxStrips feed={feed} onOpenReview={() => {}} onOpenJobs={() => {}} />,
    );
    expect(html).toContain('data-testid="inbox-merge-ready"');
    expect(html).toContain('Ready to merge (2)');
    expect(html).toContain('#12 Fix it');
    expect(html).toContain('data-testid="inbox-active-run"');
    expect(html).toContain('Active: run-td');
  });

  it('renders nothing when there is nothing actionable', () => {
    const empty: InboxFeed = {
      ...feed,
      merge_ready: { count: 0, top: [] },
      runs: { active: null, recent: [] },
    };
    const html = renderToStaticMarkup(
      <InboxStrips feed={empty} onOpenReview={() => {}} onOpenJobs={() => {}} />,
    );
    expect(html).toBe('');
  });
});

describe('agent drawer', () => {
  it('renders provider picker, prompt, and tap-to-launch idle', () => {
    const html = renderToStaticMarkup(
      <AgentDrawer repo="remote-server" initialProviders={['muse', 'agy']} />,
    );
    expect(html).toContain('data-testid="agent-drawer"');
    expect(html).toContain('data-testid="agent-provider"');
    expect(html).toContain('data-testid="agent-prompt"');
    expect(html).toContain('Launch');
    expect(html).toContain('muse');
    expect(html).not.toContain('data-testid="agent-transcript"');
  });

  it('marks dead providers and defaults to a live one', () => {
    const html = renderToStaticMarkup(
      <AgentDrawer
        repo="remote-server"
        initialProviders={['muse', 'agy']}
        initialStatus={{ muse: false, agy: true }}
      />,
    );
    expect(html).toContain('muse (unreachable)');
    expect(html).toContain('value="agy" selected');
    expect(html).not.toContain('disabled');
  });

  it('disables launch when the selected provider is dead', () => {
    const html = renderToStaticMarkup(
      <AgentDrawer
        repo="remote-server"
        initialProviders={['muse']}
        initialStatus={{ muse: false }}
      />,
    );
    expect(html).toContain('muse (unreachable)');
    expect(html).toContain('disabled');
  });

  function streamReader(text: string) {
    const bytes = new TextEncoder().encode(text);
    let sent = false;
    return {
      read: async () => {
        if (sent) return { done: true as const, value: undefined };
        sent = true;
        return { done: false as const, value: bytes };
      },
    } as unknown as ReadableStreamDefaultReader<Uint8Array>;
  }

  it('pumps tokens and resolves the done session', async () => {
    const tokens: string[] = [];
    const result = await pumpTurn(
      streamReader(
        'event: token\ndata: {"token": "AG"}\n\n' +
          'event: token\ndata: {"token": "Y-OK"}\n\n' +
          'event: done\ndata: {"text": "AGY-OK", "session_id": "abc123"}\n\n',
      ),
      (t) => tokens.push(t),
    );
    expect(tokens).toEqual(['AG', 'Y-OK']);
    expect(result).toEqual({ sessionId: 'abc123', text: 'AGY-OK', truncated: false });
  });

  it('surfaces the truncated flag from done', async () => {
    const result = await pumpTurn(
      streamReader('event: done\ndata: {"text": "big", "session_id": "s", "truncated": true}\n\n'),
      () => {},
    );
    expect(result.truncated).toBe(true);
  });

  it('rejects with the server error detail', async () => {
    await expect(
      pumpTurn(streamReader('event: error\ndata: {"error": "exit 2"}\n\n'), () => {}),
    ).rejects.toThrow('exit 2');
  });
});

describe('p2 a11y batch', () => {
  it('loading states across views use role=status', () => {
    const noop = () => {};
    expect(renderToStaticMarkup(
      <NowView repo="r" health={null} reportContext="" onAskAgent={noop} />,
    )).toContain('data-testid="now-loading" role="status"');
    expect(renderToStaticMarkup(<BriefingView repo="r" onAskAgent={noop} onOpenHistory={noop} />)).toContain(
      'data-testid="briefing-loading" role="status"',
    );
    expect(renderToStaticMarkup(<ReviewView repo="r" onAskAgent={noop} onSelectPr={() => {}} />)).toContain(
      'data-testid="review-prs-loading" role="status"',
    );
    expect(renderToStaticMarkup(<JobsView repo="r" onAskAgent={noop} />)).toContain(
      'data-testid="jobs-loading" role="status"',
    );
    expect(renderToStaticMarkup(<HistoryView repo="r" health={null} />)).toContain(
      'data-testid="history-loading" role="status"',
    );
  });

  it('report dialog carries dialog semantics and autofocus', () => {
    const html = renderToStaticMarkup(
      <ReportDialog initialBody="ctx" onSubmit={() => {}} onCancel={() => {}} />,
    );
    expect(html).toContain('role="dialog"');
    expect(html).toContain('aria-label="Report a bug"');
    expect(html).toContain('autofocus');
  });

  it('hold-to-confirm renders a polite live region for arm state', () => {
    const html = renderToStaticMarkup(
      <HoldToConfirm label="Merge it" onFire={() => {}} />,
    );
    expect(html).toContain('data-testid="hold-disarmed"');
    expect(html).toContain('data-testid="hold-notice"');
    expect(html).toContain('role="status"');
  });

  it('PR details keep heading order (h3 title, h4 prompts, no h2)', () => {
    const html = renderToStaticMarkup(
      <PrDetails
        repo="r"
        details={loadPrDetails()}
        mergeBlocked={null}
        mergeDisabled={false}
        onMerge={() => {}}
        onAskAgent={() => {}}
      />,
    );
    expect(html).toContain('<h3>');
    expect(html).toContain('<h4>Review prompts</h4>');
    expect(html).not.toContain('<h2>');
  });

  it('PR rows merge inline without opening Details', () => {
    const ready = {
      number: 7, title: 't', author: 'a', branch: 'b', isDraft: false,
      ciStatus: 'passing', ciLabel: 'passing', reviewDecision: 'APPROVED',
      mergeable: 'MERGEABLE', url: '',
    };
    const html = renderToStaticMarkup(
      <PrRowMerge pr={ready} merging={false} onMerge={() => {}} />,
    );
    expect(html).toContain('data-testid="merge-pr-7"');
    expect(html).toContain('asks for confirmation');
    expect(html).not.toContain('disabled');
  });

  it('PR row merge stays disabled when blocked', () => {
    const draft = {
      number: 8, title: 't', author: 'a', branch: 'b', isDraft: true,
      ciStatus: 'passing', ciLabel: 'passing', reviewDecision: 'APPROVED',
      mergeable: 'MERGEABLE', url: '',
    };
    const html = renderToStaticMarkup(
      <PrRowMerge pr={draft} merging={false} onMerge={() => {}} />,
    );
    expect(html).toContain('data-testid="merge-pr-8"');
    expect(html).toContain('disabled');
  });

  it('PR merge uses tap-to-fire with an inline confirm, not hold-to-arm', () => {
    const html = renderToStaticMarkup(
      <PrDetails
        repo="r"
        details={loadPrDetails()}
        mergeBlocked={null}
        mergeDisabled={false}
        onMerge={() => {}}
        onAskAgent={() => {}}
      />,
    );
    expect(html).toContain('data-testid="tap-fire"');
    expect(html).toContain('asks for confirmation');
    expect(html).not.toContain('hold-disarmed');
  });
});

describe('repo label', () => {
  const noop = () => {};

  it('reads name and branch, bare name without a branch', () => {
    expect(repoLabelText('r', 'main')).toBe('r · main');
    expect(repoLabelText('r')).toBe('r');
    expect(repoLabelText('r', '')).toBe('r');
  });

  it('shares one header format across Chat and Briefing', () => {
    const chat = renderToStaticMarkup(
      <ChatView repo="r" branch="main" seed={null} onSeedUsed={noop} />,
    );
    expect(chat).toContain('data-testid="repo-label"');
    expect(chat).toContain('r · main');
    const bare = renderToStaticMarkup(<ChatView repo="r" seed={null} onSeedUsed={noop} />);
    expect(bare).toContain('data-testid="repo-label"');
    expect(bare).not.toContain('r · ');
    expect(renderToStaticMarkup(<RepoLabel repo="r" branch="main" />)).toContain('r · main');
  });

  it('truncates long board chips with the full name on hover', () => {
    expect(truncateChip('main')).toBe('main');
    expect(truncateChip('x'.repeat(32))).toBe('x'.repeat(32));
    const long = 'feat/a-very-long-branch-name-that-keeps-going-12345';
    expect(truncateChip(long)).toBe(`${long.slice(0, 31)}…`);
    const data = {
      repo: 'r',
      attention: [],
      prs: [],
      prs_err: '',
      reviews_lines: [],
      board: [{ branch: long, issue: '', status: 'Open' }],
      board_lines: [],
      board_err: '',
      conflicts_lines: [],
      overlaps_lines: [],
      failing_runs: [],
      workflow_lines: [],
      runs_err: '',
    };
    const html = renderToStaticMarkup(
      <BriefingBody data={data} ledger={[]} onAskAgent={noop} onOpenHistory={noop} />,
    );
    expect(html).toContain(`title="${long}"`);
    expect(html).toContain(`${long.slice(0, 31)}…`);
  });
});

describe('repo picker labels', () => {
  const dirty = { name: 'remote-server', full: 'arii/remote-server', branch: 'main', clean: false };
  it('shortens the option label but keeps branch and dirty state', () => {
    const label = repoOptionLabel(dirty);
    expect(label).toContain('remote-server');
    expect(label).toContain('main');
    expect(label).toContain('dirty');
    expect(label.length).toBeLessThan('arii/remote-server · on main (dirty)'.length);
  });
  it('renders the full label as hover title', () => {
    const html = renderToStaticMarkup(
      <RepoPicker repos={[dirty]} value="remote-server" onChange={() => {}} />,
    );
    expect(html).toContain('title="arii/remote-server');
  });
});

describe('now stack condense', () => {
  const healthy = (n: number): SystemHealthResponse => ({
    disk: {},
    memory: {},
    containers: Array.from({ length: n }, (_, i) => ({
      service: `svc-${i}`,
      state: 'running',
    })),
  });
  it('collapses to one line when every container is up', () => {
    const html = renderToStaticMarkup(<StackSection health={healthy(8)} />);
    expect(html).toContain('All 8 containers up');
    expect(html).not.toContain('container-pill');
  });
  it('keeps pills when a container is down', () => {
    const h = healthy(2);
    h.containers[1] = { service: 'svc-1', state: 'exited' };
    const html = renderToStaticMarkup(<StackSection health={h} />);
    expect(html).toContain('container-pill');
  });
});

describe('triage hints', () => {
  const noop = () => {};
  it('marks hints so coarse pointers can hide them', () => {
    const html = renderToStaticMarkup(<TriageHint extra=" · d defers" />);
    expect(html).toContain('data-testid="triage-hint"');
    expect(html).toContain('triage-hint');
    expect(html).toContain('j/k move');
    expect(html).toContain('d defers');
  });
  it('wires the shared hint into Review', () => {
    const html = renderToStaticMarkup(<ReviewView repo="r" onAskAgent={noop} onSelectPr={() => {}} />);
    expect(html).toContain('data-testid="triage-hint"');
    expect(html).toContain('triage-hint');
  });
});

describe('review details prompt', () => {
  it('shows while PRs load or a PR is selectable, hides when loaded-empty', () => {
    expect(detailsPromptVisible(false, 0)).toBe(true);
    expect(detailsPromptVisible(true, 2)).toBe(true);
    expect(detailsPromptVisible(true, 0)).toBe(false);
  });
  it('renders the prompt in Review while loading', () => {
    const html = renderToStaticMarkup(<ReviewView repo="r" onAskAgent={() => {}} onSelectPr={() => {}} />);
    expect(html).toContain('data-testid="review-details-empty"');
  });
});

describe('plan dispatch', () => {
  it('builds an execute seed carrying the plan name and content', () => {
    const seed = planExecuteSeed('2026-11-03-plan.md', '## Steps\n1. Merge #1');
    expect(seed).toContain('Execute');
    expect(seed).toContain('2026-11-03-plan.md');
    expect(seed).toContain('Merge #1');
  });
  it('wires the agent opener into Jobs', () => {
    const html = renderToStaticMarkup(<JobsView repo="r" onAskAgent={() => {}} />);
    expect(html).toContain('data-testid="jobs-loading"');
  });
});
