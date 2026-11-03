import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { isStalled, meteredTotal, runCost, runDecisions, RunsPane, STALL_SECONDS, StreamProgress, streamProgress } from './ChatView';
import type { AgentRun, AgentRunDetail } from '../apiClient';

describe('stream progress (issue #376)', () => {
  it('formats elapsed alone before anything arrives', () => {
    expect(streamProgress(0, null, 0)).toBe('Working… 0s');
    expect(streamProgress(45, null, 0)).toBe('Working… 45s');
  });

  it('appends phase and token count', () => {
    expect(streamProgress(45, 'prefill', 0)).toBe('Working… 45s · prefill');
    expect(streamProgress(45, 'prefill', 12)).toBe('Working… 45s · prefill · 12 tok');
    expect(streamProgress(45, null, 12)).toBe('Working… 45s · 12 tok');
  });

  it('renders the progress line', () => {
    const html = renderToStaticMarkup(
      <StreamProgress elapsed={45} phase="prefill" tokens={12} />,
    );
    expect(html).toContain('data-testid="chat-progress"');
    expect(html).toContain('Working… 45s · prefill · 12 tok');
  });
});

describe('stall watchdog (issue #376)', () => {
  it('flags streams silent past the stall window', () => {
    expect(STALL_SECONDS).toBe(180);
    expect(isStalled(0, 179_999)).toBe(false);
    expect(isStalled(0, 180_000)).toBe(false);
    expect(isStalled(0, 180_001)).toBe(true);
  });
});

function pastRun(over: Partial<AgentRun> = {}): AgentRun {
  return {
    id: 'run-0',
    script: 'deep_chat.py',
    model: 'm',
    thread: 't',
    started_ts: 0,
    duration_s: 1,
    ok: true,
    tools: [],
    tokens: 0,
    preview: 'hello',
    has_prompt: true,
    cost_usd: 0.0,
    metered: false,
    ...over,
  };
}

function paneHtml(runs: AgentRun[], notice: string | null = null): string {
  return renderToStaticMarkup(
    <RunsPane
      runs={runs}
      selected={null}
      detail={null}
      onSelect={() => {}}
      onContinue={() => {}}
      onDelete={() => {}}
      onClearFinished={() => {}}
      notice={notice}
    />,
  );
}

describe('past runs manage (issue #412)', () => {
  it('renders continue and delete per row plus clear finished', () => {
    const html = paneHtml([pastRun()]);
    expect(html).toContain('data-testid="chat-run-continue-run-0"');
    expect(html).toContain('>Continue</button>');
    expect(html).toContain('data-testid="chat-run-delete-run-0"');
    expect(html).toContain('data-testid="chat-runs-clear"');
    expect(html).toContain('>Clear finished</button>');
  });

  it('disables continue when the run has no stored prompt', () => {
    const html = paneHtml([pastRun({ id: 'run-1', has_prompt: false })]);
    expect(html).toContain('data-testid="chat-run-continue-run-1" disabled=""');
    expect(html).toContain('No stored prompt for this run');
  });

  it('enables continue when the run has a stored prompt', () => {
    const html = paneHtml([pastRun({ has_prompt: true })]);
    expect(html).not.toContain('disabled=""');
  });

  it('shows the runs notice', () => {
    const html = paneHtml([pastRun()], 'Deleted run-0.');
    expect(html).toContain('data-testid="chat-runs-notice"');
    expect(html).toContain('Deleted run-0.');
  });

  it('hides the pane when there are no runs', () => {
    expect(paneHtml([])).toBe('');
  });
});

function detailWith(extra: Record<string, unknown>): AgentRunDetail {
  return { id: 'run-0', entry: { thread_id: 't', extra }, trace: [] };
}

describe('run decisions (issue #414)', () => {
  it('parses decisions from the entry extra', () => {
    const entry = {
      extra: {
        decisions: [
          { what: 'ran grep_repo(q)', why: '→ found it' },
          { what: 'answered from 1 tool result', why: 'The answer.' },
        ],
      },
    };
    expect(runDecisions(entry)).toEqual([
      { what: 'ran grep_repo(q)', why: '→ found it' },
      { what: 'answered from 1 tool result', why: 'The answer.' },
    ]);
  });

  it('returns empty when decisions are absent or malformed', () => {
    expect(runDecisions({})).toEqual([]);
    expect(runDecisions({ extra: {} })).toEqual([]);
    expect(runDecisions({ extra: { decisions: 'nope' } })).toEqual([]);
    expect(runDecisions({ extra: { decisions: [{ what: 1 }] } })).toEqual([]);
  });

  it('lists decisions with rationale in run detail', () => {
    const html = renderToStaticMarkup(
      <RunsPane
        runs={[pastRun()]}
        selected="run-0"
        detail={detailWith({
          decisions: [{ what: 'ran grep_repo(q)', why: '→ found it' }],
        })}
        onSelect={() => {}}
        onContinue={() => {}}
        onDelete={() => {}}
        onClearFinished={() => {}}
        notice={null}
      />,
    );
    expect(html).toContain('data-testid="chat-run-decisions"');
    expect(html).toContain('ran grep_repo(q)');
    expect(html).toContain('→ found it');
  });

  it('omits the decisions section without stored decisions', () => {
    const html = renderToStaticMarkup(
      <RunsPane
        runs={[pastRun()]}
        selected="run-0"
        detail={detailWith({})}
        onSelect={() => {}}
        onContinue={() => {}}
        onDelete={() => {}}
        onClearFinished={() => {}}
        notice={null}
      />,
    );
    expect(html).not.toContain('chat-run-decisions');
  });
});

describe('run cost (issue #398)', () => {
  it('tags local runs free', () => {
    expect(runCost(pastRun({ metered: false, cost_usd: 0.0 }))).toBe('free');
  });

  it('prices metered runs', () => {
    expect(runCost(pastRun({ metered: true, cost_usd: 0.0008 }))).toBe(
      '$0.0008',
    );
  });

  it('marks unrated metered runs without inventing a number', () => {
    expect(runCost(pastRun({ metered: true, cost_usd: null }))).toBe(
      'metered',
    );
  });

  it('sums the metered total and counts unknown rates', () => {
    expect(
      meteredTotal([
        pastRun({ metered: false, cost_usd: 0.0 }),
        pastRun({ id: 'run-1', metered: true, cost_usd: 0.0008 }),
        pastRun({ id: 'run-2', metered: true, cost_usd: 0.0002 }),
        pastRun({ id: 'run-3', metered: true, cost_usd: null }),
      ]),
    ).toEqual({ total: 0.001, unknown: 1, free: 1 });
  });

  it('shows per-row cost and the metered total', () => {
    const html = paneHtml([
      pastRun({ metered: true, cost_usd: 0.0008 }),
      pastRun({ id: 'run-1', metered: false, cost_usd: 0.0 }),
    ]);
    expect(html).toContain('$0.0008');
    expect(html).toContain('free');
    expect(html).toContain('data-testid="chat-runs-metered-total"');
    expect(html).toContain('Metered total $0.0008');
  });

  it('flags unknown-rate runs in the total', () => {
    const html = paneHtml([
      pastRun({ metered: true, cost_usd: null }),
    ]);
    expect(html).toContain('metered');
    expect(html).toContain('(+1 unknown rate)');
  });
});
