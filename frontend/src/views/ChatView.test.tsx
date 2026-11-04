import { describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { budgetLine, ChatHistory, CHAT_PROMPT_CHIPS, chipPickAction, insertMention, isStalled, MentionDropdown, mentionQueryAt, meteredTotal, PromptChips, refreshRunsQuiet, runCost, runDecisions, RunsPane, shouldShowChatEdit, shouldShowChatRegenerate, shouldShowChatRetry, STALL_SECONDS, StreamProgress, streamProgress, turnPrompt } from './ChatView';
import type { BudgetStatus } from '../apiClient';
import type { AgentRun, AgentRunDetail } from '../apiClient';
import type { ChatMessage } from './mockViews';

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

describe('chat retry visibility (issue #433)', () => {
  it('shows retry only after an error with something to re-send', () => {
    expect(shouldShowChatRetry('network error', false, 'hi')).toBe(true);
    expect(shouldShowChatRetry(null, false, 'hi')).toBe(false);
    expect(shouldShowChatRetry('', false, 'hi')).toBe(false);
    expect(shouldShowChatRetry('network error', true, 'hi')).toBe(false);
    expect(shouldShowChatRetry('network error', false, '')).toBe(false);
    expect(shouldShowChatRetry('network error', false, '   ')).toBe(false);
  });
});

function historyHtml(messages: ChatMessage[], streaming = false): string {
  return renderToStaticMarkup(
    <ChatHistory
      messages={messages}
      streaming={streaming}
      onEdit={() => {}}
      onRegenerate={() => {}}
    />,
  );
}

const twoTurns: ChatMessage[] = [
  { role: 'user', text: 'first prompt' },
  { role: 'assistant', text: 'first reply' },
  { role: 'user', text: 'second prompt' },
  { role: 'assistant', text: 'second reply' },
];

describe('chat edit affordance (issue #629)', () => {
  it('shows edit on settled streams only', () => {
    expect(shouldShowChatEdit(false)).toBe(true);
    expect(shouldShowChatEdit(true)).toBe(false);
  });

  it('renders an edit button on every sent user bubble', () => {
    const html = historyHtml(twoTurns);
    expect(html).toContain('data-testid="chat-edit-0"');
    expect(html).toContain('data-testid="chat-edit-2"');
    expect(html).toContain('>Edit</button>');
    expect(html).not.toContain('chat-edit-1');
    expect(html).not.toContain('chat-edit-3');
  });

  it('hides every edit button while streaming', () => {
    expect(historyHtml(twoTurns, true)).not.toContain('chat-edit-');
  });
});

describe('chat regenerate affordance (issue #629)', () => {
  it('recovers the prompt of each turn, not the global last sent', () => {
    expect(turnPrompt(twoTurns, 1)).toBe('first prompt');
    expect(turnPrompt(twoTurns, 3)).toBe('second prompt');
  });

  it('returns empty when no user turn precedes the bubble', () => {
    expect(turnPrompt([{ role: 'assistant', text: 'orphan' }], 0)).toBe('');
    expect(turnPrompt([], 0)).toBe('');
  });

  it('gates regenerate on settled stream, reply text, and recoverable prompt', () => {
    expect(shouldShowChatRegenerate(false, 'a reply', 'a prompt')).toBe(true);
    expect(shouldShowChatRegenerate(true, 'a reply', 'a prompt')).toBe(false);
    expect(shouldShowChatRegenerate(false, '', 'a prompt')).toBe(false);
    expect(shouldShowChatRegenerate(false, '   ', 'a prompt')).toBe(false);
    expect(shouldShowChatRegenerate(false, 'a reply', '')).toBe(false);
    expect(shouldShowChatRegenerate(false, 'a reply', '   ')).toBe(false);
  });

  it('renders regenerate on completed replies with a recoverable prompt', () => {
    const html = historyHtml(twoTurns);
    expect(html).toContain('data-testid="chat-regenerate-1"');
    expect(html).toContain('data-testid="chat-regenerate-3"');
    expect(html).toContain('>Regenerate</button>');
    expect(html).not.toContain('chat-regenerate-0');
    expect(html).not.toContain('chat-regenerate-2');
  });

  it('hides every regenerate button while streaming', () => {
    expect(historyHtml(twoTurns, true)).not.toContain('chat-regenerate-');
  });

  it('hides regenerate when no prompt is recoverable', () => {
    const html = historyHtml([{ role: 'assistant', text: 'orphan reply' }]);
    expect(html).toContain('chat-msg-assistant-0');
    expect(html).not.toContain('chat-regenerate-');
  });

  it('hides regenerate on an empty reply', () => {
    const html = historyHtml([
      { role: 'user', text: 'a prompt' },
      { role: 'assistant', text: '' },
    ]);
    expect(html).not.toContain('chat-regenerate-');
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

function paneHtml(
  runs: AgentRun[],
  notice: string | null = null,
  budget: BudgetStatus | null = null,
): string {
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
      budget={budget}
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

function selectedPaneHtml(runs: AgentRun[], selected: string | null, detail: AgentRunDetail | null): string {
  return renderToStaticMarkup(
    <RunsPane
      runs={runs}
      selected={selected}
      detail={detail}
      onSelect={() => {}}
      onContinue={() => {}}
      onDelete={() => {}}
      onClearFinished={() => {}}
      notice={null}
      budget={null}
    />,
  );
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
        budget={null}
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
        budget={null}
      />,
    );
    expect(html).not.toContain('chat-run-decisions');
  });
});

describe('selected past run (issue #567)', () => {
  const runs = [pastRun({ id: 'run-1' }), pastRun({ id: 'run-0' })];

  it('marks exactly the selected card with the selected class', () => {
    const html = selectedPaneHtml(runs, 'run-1', detailWith({}));
    expect(html.match(/class="detail selected"/g)).toHaveLength(1);
    const rowAt = html.indexOf('data-testid="chat-run-run-1"');
    const selectedAt = html.indexOf('class="detail selected"');
    expect(selectedAt).toBeGreaterThanOrEqual(0);
    expect(selectedAt).toBeLessThan(rowAt);
  });

  it('renders the detail adjacent to the selected row, not after the list', () => {
    const html = selectedPaneHtml(runs, 'run-1', detailWith({}));
    const rowAt = html.indexOf('data-testid="chat-run-run-1"');
    const detailAt = html.indexOf('data-testid="chat-run-detail"');
    const nextRowAt = html.indexOf('data-testid="chat-run-run-0"');
    expect(detailAt).toBeGreaterThan(rowAt);
    expect(detailAt).toBeLessThan(nextRowAt);
  });

  it('shows no selection and no detail when nothing is selected', () => {
    const html = selectedPaneHtml(runs, null, null);
    expect(html).not.toContain('detail selected');
    expect(html).not.toContain('data-testid="chat-run-detail"');
  });

  it('marks the row even while its detail is still loading', () => {
    const html = selectedPaneHtml(runs, 'run-0', null);
    expect(html.match(/class="detail selected"/g)).toHaveLength(1);
    expect(html).not.toContain('data-testid="chat-run-detail"');
  });
});

describe('past runs refresh after send (issue #568)', () => {
  it('replaces the list with the fresh fetch (no duplicate rows)', async () => {
    const fresh = [pastRun({ id: 'run-8' }), pastRun({ id: 'run-7' })];
    const setRuns = vi.fn();
    await refreshRunsQuiet(async () => ({ runs: fresh }), setRuns);
    expect(setRuns).toHaveBeenCalledTimes(1);
    expect(setRuns).toHaveBeenCalledWith(fresh);
  });

  it('leaves the list unchanged when the refetch fails', async () => {
    const setRuns = vi.fn();
    await expect(
      refreshRunsQuiet(async () => {
        throw new Error('network down');
      }, setRuns),
    ).resolves.toBeUndefined();
    expect(setRuns).not.toHaveBeenCalled();
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

function budget(over: Partial<BudgetStatus> = {}): BudgetStatus {
  return {
    cap_usd: 5.0,
    spent_usd: 0.0008,
    remaining_usd: 4.9992,
    blocked: false,
    unknown: 0,
    ...over,
  };
}

describe('spend budget (issue #409)', () => {
  it('shows remaining budget when capped', () => {
    expect(budgetLine(budget())).toBe('Budget $4.9992 left of $5');
  });

  it('says plainly when no cap is set', () => {
    expect(
      budgetLine(budget({ cap_usd: null, remaining_usd: null })),
    ).toBe('No spend cap set');
  });

  it('states the block with figures when capped out', () => {
    expect(
      budgetLine(
        budget({ cap_usd: 1.0, spent_usd: 1.2, remaining_usd: 0.0, blocked: true }),
      ),
    ).toBe('Budget cap hit — metered calls blocked ($1.2 of $1 used)');
  });

  it('renders the budget line in the pane', () => {
    const html = paneHtml([pastRun()], null, budget());
    expect(html).toContain('data-testid="chat-runs-budget"');
    expect(html).toContain('Budget $4.9992 left of $5');
  });

  it('omits the budget line until loaded', () => {
    expect(paneHtml([pastRun()], null, null)).not.toContain(
      'chat-runs-budget',
    );
  });
});

describe('prompt chips (issue #664)', () => {
  it('offers the three starters with stable testids', () => {
    expect(CHAT_PROMPT_CHIPS.map((c) => c.id)).toEqual(['review', 'todos', 'diff']);
    expect(CHAT_PROMPT_CHIPS.every((c) => c.prompt.trim().length > 0)).toBe(true);
    const html = renderToStaticMarkup(<PromptChips disabled={false} onPick={() => {}} />);
    expect(html).toContain('data-testid="chat-prompt-chips"');
    expect(html).toContain('data-testid="chat-chip-review"');
    expect(html).toContain('data-testid="chat-chip-todos"');
    expect(html).toContain('data-testid="chat-chip-diff"');
    expect(html).toContain('Review uncommitted changes');
    expect(html).toContain('Alt+click fills without sending');
    expect(html).not.toContain('disabled');
  });

  it('disables chips while streaming', () => {
    const html = renderToStaticMarkup(<PromptChips disabled onPick={() => {}} />);
    expect(html).toContain('disabled');
  });

  it('tap sends, alt-click fills, streaming blocks', () => {
    expect(chipPickAction('Review my uncommitted changes', false, false)).toBe('send');
    expect(chipPickAction('Review my uncommitted changes', true, false)).toBe('fill');
    expect(chipPickAction('Review my uncommitted changes', false, true)).toBe('none');
    expect(chipPickAction('Review my uncommitted changes', true, true)).toBe('none');
    expect(chipPickAction('   ', false, false)).toBe('none');
  });
});

describe('@-mention completion (issue #653)', () => {
  it('finds the query ending at the caret', () => {
    expect(mentionQueryAt('look @sub/a', 11)).toEqual({ start: 5, query: 'sub/a' });
    expect(mentionQueryAt('@', 1)).toEqual({ start: 0, query: '' });
    expect(mentionQueryAt('(@a', 3)).toEqual({ start: 1, query: 'a' });
    expect(mentionQueryAt('hi @a there', 5)).toEqual({ start: 3, query: 'a' });
  });

  it('ignores mid-word @, finished tokens, and empty heads', () => {
    expect(mentionQueryAt('user@example.com', 16)).toBe(null);
    expect(mentionQueryAt('a@b', 3)).toBe(null);
    expect(mentionQueryAt('hi @a there', 4)).toBe(null);
    expect(mentionQueryAt('hi @a there', 6)).toBe(null);
    expect(mentionQueryAt('hi @a there', 3)).toBe(null);
    expect(mentionQueryAt('', 0)).toBe(null);
    expect(mentionQueryAt('@a', 0)).toBe(null);
  });

  it('inserts the picked path as chip text with a trailing space', () => {
    expect(insertMention('look @sub/a', 11, 'sub/a.py')).toEqual({
      text: 'look @sub/a.py ',
      cursor: 15,
    });
    expect(insertMention('@', 1, 'README.md')).toEqual({
      text: '@README.md ',
      cursor: 11,
    });
  });

  it('refuses insertion without an active query', () => {
    expect(insertMention('plain', 5, 'x.py')).toBe(null);
    expect(insertMention('user@example.com', 16, 'x.py')).toBe(null);
  });

  it('renders options with the active one marked', () => {
    const html = renderToStaticMarkup(
      <MentionDropdown
        files={['sub/a.py', 'sub/b.py']}
        truncated={false}
        active={1}
        onPick={() => {}}
        onHover={() => {}}
      />,
    );
    expect(html).toContain('data-testid="chat-mention-list"');
    expect(html).toContain('sub/a.py');
    expect(html).toContain('sub/b.py');
    expect(html).toContain('aria-selected="true"');
    expect(html).not.toContain('chat-mention-more');
  });

  it('shows the refine-prefix hint when truncated', () => {
    const html = renderToStaticMarkup(
      <MentionDropdown
        files={['a.py']}
        truncated
        active={0}
        onPick={() => {}}
        onHover={() => {}}
      />,
    );
    expect(html).toContain('data-testid="chat-mention-more"');
    expect(html).toContain('Refine the prefix for more');
  });
});
