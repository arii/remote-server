import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import ChatView, { appendDictationText, applyDictationResult, budgetLine, CHAT_BACKEND_OPTIONS, CHAT_DRAFT_KEY, CHAT_MESSAGES_KEY, CHAT_SESSION_KEY, ChatHistory, CHAT_PROMPT_CHIPS, chipPickAction, clearChatDraft, clearChatSessionId, dictationCtor, dictationErrorHint, dictationSupported, DictateButton, DictationHint, extractDictationText, insertMention, interruptedPrompt, isChatMessage, isStalled, isTurnInProgressError, loadChatDraft, loadChatMessages, loadChatSessionId, matchSlashCommands, MentionDropdown, mentionQueryAt, meteredTotal, PromptChips, pumpStream, repoMetricsLine, RepoMetricsStrip, refreshRunsQuiet, resolveChatSessionId, restoreUserBubble, resumeActionForStatus, runActionLabel, runCost, runDecisions, runTitle, runLatency, runMatchesRepo, filterRunsByRepo, shortRunRepo, runMeta, RunsPane, saveChatDraft, saveChatMessages, saveChatSessionId, settledChatMessages, shouldAutoRetryStall, shouldShowChatEdit, shouldShowChatRegenerate, shouldShowChatRetry, SLASH_COMMANDS, SlashMenu, slashQueryAt, STALL_SECONDS, stageSeed, StreamProgress, streamProgress, stripFailedRetryExchange, CHAT_TEMPLATES_KEY, insertTemplateAt, loadPromptTemplates, makeTemplateFromDraft, savePromptTemplates, SEED_TEMPLATES, TEMPLATE_TEXT_SLOT, TemplatePanel, templateLabelForDraft, TraceTimeline, transcriptFilename, transcriptMarkdown, turnPrompt } from './ChatView';
import type { DictationResultEvent, SlashActions } from './ChatView';
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

describe('chat retry reuses the pending exchange (issue #587)', () => {
  it('drops the trailing failed empty pair', () => {
    const messages: ChatMessage[] = [
      { role: 'user', text: 'Summarize the open pull requests' },
      { role: 'assistant', text: '' },
    ];
    expect(stripFailedRetryExchange(messages, 'Summarize the open pull requests')).toEqual([]);
  });

  it('drops the trailing failed partial pair, keeping earlier turns', () => {
    const messages: ChatMessage[] = [
      { role: 'user', text: 'first prompt' },
      { role: 'assistant', text: 'first reply' },
      { role: 'user', text: 'second prompt' },
      { role: 'assistant', text: 'partial…' },
    ];
    expect(stripFailedRetryExchange(messages, 'second prompt')).toEqual([
      { role: 'user', text: 'first prompt' },
      { role: 'assistant', text: 'first reply' },
    ]);
  });

  it('repeated retries converge on one pair, not one per attempt', () => {
    const sent = 'Summarize the open pull requests';
    let messages: ChatMessage[] = [];
    for (let attempt = 0; attempt < 3; attempt++) {
      messages = stripFailedRetryExchange(messages, sent);
      messages = [...messages, { role: 'user', text: sent }, { role: 'assistant', text: '' }];
    }
    expect(messages.filter((m) => m.role === 'user')).toHaveLength(1);
  });

  it('leaves completed history untouched', () => {
    const messages: ChatMessage[] = [
      { role: 'user', text: 'first prompt' },
      { role: 'assistant', text: 'first reply' },
    ];
    expect(stripFailedRetryExchange(messages, 'other prompt')).toBe(messages);
    // A matching trailing pair is always the failed exchange when
    // retry is clickable (error non-null), even with partial text.
    expect(stripFailedRetryExchange(messages, 'first prompt')).toEqual([]);
    expect(stripFailedRetryExchange([], 'hi')).toEqual([]);
    expect(stripFailedRetryExchange([{ role: 'user', text: 'hi' }], 'hi')).toEqual([
      { role: 'user', text: 'hi' },
    ]);
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

    expect(interruptedPrompt([])).toBeNull();
    expect(
      interruptedPrompt([{ role: 'assistant', text: 'done' }]),
    ).toBeNull();
    expect(
      interruptedPrompt([
        { role: 'user', text: 'q?' },
        { role: 'assistant', text: '' },
      ]),
    ).toBeNull();
    expect(
      interruptedPrompt([
        { role: 'assistant', text: 'done' },
        { role: 'user', text: 'stranded?' },
      ]),
    ).toBe('stranded?');
    expect(interruptedPrompt([{ role: 'user', text: '   ' }])).toBeNull();
    expect(shouldAutoRetryStall(false)).toBe(true);
    expect(shouldAutoRetryStall(true)).toBe(false);
  });
});

describe('409 means attach, not error (issue #1130)', () => {
  it('detects the turn-in-progress rejection', () => {
    expect(isTurnInProgressError(new Error('Chat stream failed: 409'))).toBe(true);
    expect(isTurnInProgressError('request failed: 409 Conflict')).toBe(true);
  });
  it('ignores other failures', () => {
    expect(isTurnInProgressError(new Error('Chat stream failed: 500'))).toBe(false);
    expect(isTurnInProgressError(new Error('network down'))).toBe(false);
    expect(isTurnInProgressError(null)).toBe(false);
  });
  it('rejects boundary lookalikes', () => {
    expect(isTurnInProgressError(new Error('Chat stream failed: 4090'))).toBe(false);
    expect(isTurnInProgressError(new Error('budget exceeded: 4096 tokens'))).toBe(false);
  });
});

describe('409 attach keeps the user bubble (issue #1130)', () => {
  it('restores the prompt after a strip', () => {
    const stripped = stripFailedRetryExchange(
      [
        { role: 'user', text: 'hello' },
        { role: 'assistant', text: 'partial' },
      ],
      'hello',
    );
    expect(restoreUserBubble(stripped, 'hello')).toEqual([{ role: 'user', text: 'hello' }]);
  });
  it('leaves an existing trailing bubble alone', () => {
    const messages: ChatMessage[] = [{ role: 'user', text: 'hello' }];
    expect(restoreUserBubble(messages, 'hello')).toBe(messages);
  });
  it('appends to an empty log without duplicating a different prompt', () => {
    expect(restoreUserBubble([], 'hello')).toEqual([{ role: 'user', text: 'hello' }]);
    expect(
      restoreUserBubble([{ role: 'user', text: 'other' }], 'hello'),
    ).toEqual([{ role: 'user', text: 'other' }, { role: 'user', text: 'hello' }]);
  });
});

describe('remount resume decision (issue #1015)', () => {
  const base = { session_id: 'abc123', phase: 'generating', text: '', elapsed: 3 };
  it('streams live turns', () => {
    expect(resumeActionForStatus({ ...base, state: 'live' }, 'abc123')).toEqual({
      kind: 'stream',
    });
  });
  it('backfills turns that finished while the page was gone', () => {
    expect(resumeActionForStatus({ ...base, state: 'done', text: 'hi' }, 'abc123')).toEqual({
      kind: 'backfill',
      text: 'hi',
    });
    expect(
      resumeActionForStatus({ ...base, state: 'aborted', text: 'part' }, 'abc123'),
    ).toEqual({ kind: 'backfill', text: 'part' });
  });
  it('restores the approval card', () => {
    expect(
      resumeActionForStatus(
        { ...base, state: 'approval_pending', approval: { session_id: 'abc123' } },
        'abc123',
      ),
    ).toEqual({ kind: 'approval' });
  });
  it('leaves the banner for empty finishes, errors, and strangers', () => {
    expect(resumeActionForStatus({ ...base, state: 'done', text: '' }, 'abc123')).toEqual({
      kind: 'none',
    });
    expect(resumeActionForStatus({ ...base, state: 'error', text: '' }, 'abc123')).toEqual({
      kind: 'none',
    });
    expect(resumeActionForStatus({ ...base, state: 'live' }, 'other')).toEqual({
      kind: 'none',
    });
    expect(resumeActionForStatus(null, 'abc123')).toEqual({ kind: 'none' });
    expect(
      resumeActionForStatus({ ...base, state: 'approval_pending' }, 'abc123'),
    ).toEqual({ kind: 'none' });
  });
});

function pastRun(over: Partial<AgentRun> = {}): AgentRun {
  return {
    id: 'run-0',
    script: 'deep_chat.py',
    model: 'm',
    provider: null,
    thread: 't',
    repo: null,
    started_ts: 0,
    duration_s: 1,
    ok: true,
    tools: [],
    tokens: 0,
    preview: 'hello',
    prompt_first_line: '',
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
  repo = 'r',
  showAllRepos = false,
): string {
  return renderToStaticMarkup(
    <RunsPane
      runs={runs}
      repo={repo}
      showAllRepos={showAllRepos}
      onToggleScope={() => {}}
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

describe('run row action accessible names (issue #580)', () => {
  it('names Continue/Delete with the run id', () => {
    const html = paneHtml([pastRun({ id: 'run-7' })]);
    expect(html).toContain('aria-label="Continue run-7"');
    expect(html).toContain('aria-label="Delete run-7 (asks for confirmation)"');
  });

  it('keeps visible text as the bare verbs', () => {
    const html = paneHtml([pastRun({ id: 'run-7' })]);
    expect(html).toContain('>Continue</button>');
    expect(html).toContain('>Delete</button>');
    expect(html).not.toContain('>Continue run-7<');
    expect(html).not.toContain('>Delete run-7<');
  });

  it('gives every repeated row action a unique name', () => {
    const html = paneHtml([pastRun({ id: 'run-0' }), pastRun({ id: 'run-1' })]);
    const names = [...html.matchAll(/aria-label="((?:Continue|Delete)[^"]+)"/g)].map((m) => m[1]);
    expect(names).toHaveLength(4);
    expect(new Set(names).size).toBe(4);
  });
});

function detailWith(extra: Record<string, unknown>): AgentRunDetail {
  return { id: 'run-0', entry: { thread_id: 't', extra }, trace: [] };
}

function selectedPaneHtml(runs: AgentRun[], selected: string | null, detail: AgentRunDetail | null): string {
  return renderToStaticMarkup(
    <RunsPane
      runs={runs}
      repo="r"
      showAllRepos={false}
      onToggleScope={() => {}}
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
        repo="r"
        showAllRepos={false}
        onToggleScope={() => {}}
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
        repo="r"
        showAllRepos={false}
        onToggleScope={() => {}}
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

describe('run titles (issue #960)', () => {
  it('prefers the final preview', () => {
    expect(runTitle(pastRun({ preview: 'answer here', prompt_first_line: 'fix it' }))).toBe('answer here');
  });

  it('falls back to the first prompt line', () => {
    expect(runTitle(pastRun({ id: 'run-9', preview: '', prompt_first_line: 'fix the deploy' }))).toBe('fix the deploy');
  });

  it('falls back to the run id only when no words exist', () => {
    expect(runTitle(pastRun({ id: 'run-9', preview: '', prompt_first_line: '' }))).toBe('run-9');
  });

  it('renders the prompt fallback in the pane', () => {
    const html = paneHtml([pastRun({ id: 'run-9', preview: '', prompt_first_line: 'fix the deploy' })]);
    expect(html).toContain('fix the deploy');
    expect(html).not.toContain('>run-9<');
  });
});

describe('run action labels (issue #1109.7)', () => {
  const BLOB = "**console-app** - triage_counts: 'open_prs: 2…";

  it('replaces a data-blob preview with the human action label', () => {
    const run = pastRun({ script: 'run_triage.py', preview: BLOB, prompt_first_line: '' });
    expect(runTitle(run)).toBe('Run Triage');
    expect(runTitle(run)).not.toContain('triage_counts');
  });

  it('keeps an ordinary prose preview untouched', () => {
    expect(runTitle(pastRun({ preview: 'fixed the deploy', prompt_first_line: '' }))).toBe(
      'fixed the deploy',
    );
  });

  it('still falls back to the prompt line over the action label', () => {
    const run = pastRun({
      script: 'run_triage.py',
      preview: BLOB,
      prompt_first_line: 'triage console-app',
    });
    expect(runTitle(run)).toBe('triage console-app');
  });

  it('falls back to the run id when no words exist', () => {
    expect(runTitle(pastRun({ id: 'run-9', script: '', preview: '', prompt_first_line: '' }))).toBe(
      'run-9',
    );
  });

  it('humanises script file names', () => {
    expect(runActionLabel(pastRun({ script: 'run_triage.py' }))).toBe('Run Triage');
    expect(runActionLabel(pastRun({ script: 'deep_chat.py' }))).toBe('Deep Chat');
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

describe('run provider/latency slots (issue #801)', () => {
  it('formats recorded latency, dashes the unknown', () => {
    expect(runLatency(pastRun({ duration_s: 12.5 }))).toBe('12.5s');
    expect(runLatency(pastRun({ duration_s: null }))).toBe('—');
  });

  it('shows provider, latency, and cost on metered rows', () => {
    const meta = runMeta(
      pastRun({
        model: 'nebius:zai-org/GLM-5.3-Flash',
        provider: 'nebius',
        duration_s: 12.5,
        tokens: 3000,
        metered: true,
        cost_usd: 0.0008,
      }),
    );
    expect(meta).toContain('nebius · zai-org/GLM-5.3-Flash');
    expect(meta).not.toContain('nebius:zai-org');
    expect(meta).toContain('12.5s');
    expect(meta).toContain('3000 tok');
    expect(meta).toContain('$0.0008');
  });

  it('stays null-safe on local rows with unknown latency', () => {
    const meta = runMeta(
      pastRun({
        model: 'ollama:qwen3:8b',
        provider: 'ollama',
        duration_s: null,
        metered: false,
        cost_usd: 0.0,
      }),
    );
    expect(meta).toContain('ollama · qwen3:8b');
    expect(meta).toContain('—');
    expect(meta).toContain('free');
    expect(meta).not.toContain('null');
  });

  it('shows bare models verbatim without a provider slot', () => {
    const meta = runMeta(pastRun({ model: 'qwen3:8b', provider: null }));
    expect(meta).toContain('deep_chat.py · qwen3:8b · ok');
  });

  it('renders the slots in the pane', () => {
    const html = paneHtml([
      pastRun({
        model: 'nebius:zai-org/GLM-5.3-Flash',
        provider: 'nebius',
        duration_s: 12.5,
        metered: true,
        cost_usd: 0.0008,
      }),
      pastRun({
        id: 'run-1',
        model: 'ollama:qwen3:8b',
        provider: 'ollama',
        duration_s: null,
        metered: false,
        cost_usd: 0.0,
      }),
    ]);
    expect(html).toContain('nebius · zai-org/GLM-5.3-Flash');
    expect(html).toContain('ollama · qwen3:8b');
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

describe('composer slash commands (issue #669)', () => {
  it('holds the v1 table in one const', () => {
    expect(SLASH_COMMANDS.map((c) => c.cmd)).toEqual([
      'retry',
      'clear',
      'model',
      'attach',
      'export',
    ]);
    for (const c of SLASH_COMMANDS) {
      expect(c.label.length).toBeGreaterThan(0);
      expect(c.hint.length).toBeGreaterThan(0);
    }
  });

  it('routes each command to its own UI action only', () => {
    const calls: Record<keyof SlashActions, ReturnType<typeof vi.fn>> = {
      retry: vi.fn(),
      clear: vi.fn(),
      focusModel: vi.fn(),
      attach: vi.fn(),
      exportTranscript: vi.fn(),
    };
    const byCmd: Record<string, keyof SlashActions> = {
      retry: 'retry',
      clear: 'clear',
      model: 'focusModel',
      attach: 'attach',
      export: 'exportTranscript',
    };
    for (const c of SLASH_COMMANDS) {
      for (const fn of Object.values(calls)) fn.mockClear();
      c.run(calls);
      for (const [name, fn] of Object.entries(calls)) {
        expect(fn).toHaveBeenCalledTimes(name === byCmd[c.cmd] ? 1 : 0);
      }
    }
  });

  it('opens on a leading slash only', () => {
    expect(slashQueryAt('/')).toBe('');
    expect(slashQueryAt('/re')).toBe('re');
    expect(slashQueryAt('a/b')).toBe(null);
    expect(slashQueryAt('use /retry here')).toBe(null);
    expect(slashQueryAt('  /retry')).toBe(null);
    expect(slashQueryAt('/retry now')).toBe(null);
    expect(slashQueryAt('')).toBe(null);
  });

  it('filters case-insensitively, unknown tokens match nothing', () => {
    expect(matchSlashCommands('')).toHaveLength(5);
    expect(matchSlashCommands('r').map((c) => c.cmd)).toEqual(['retry']);
    expect(matchSlashCommands('RE').map((c) => c.cmd)).toEqual(['retry']);
    expect(matchSlashCommands('e').map((c) => c.cmd)).toEqual(['export']);
    // Unknown "/foo": token recognized, zero matches, so the menu
    // stays shut and the text sends as a plain prompt.
    expect(slashQueryAt('/foo')).toBe('foo');
    expect(matchSlashCommands('foo')).toEqual([]);
  });

  it('renders the menu as a labelled listbox with the active option marked', () => {
    const html = renderToStaticMarkup(
      <SlashMenu
        commands={matchSlashCommands('')}
        active={2}
        onPick={() => {}}
        onHover={() => {}}
      />,
    );
    expect(html).toContain('data-testid="chat-slash-menu"');
    expect(html).toContain('role="listbox"');
    expect(html).toContain('aria-label="Composer commands"');
    expect(html).toContain('role="option"');
    for (const c of SLASH_COMMANDS) {
      expect(html).toContain(`/${c.cmd}`);
      expect(html).toContain(c.hint);
    }
    expect(html).toContain('aria-selected="true"');
    expect(html).toContain('data-active="true"');
  });
});

describe('chat transcript export (issue #669)', () => {
  it('renders turns in order as Markdown with cleaned assistant text', () => {
    const messages: ChatMessage[] = [
      { role: 'user', text: 'first prompt' },
      { role: 'assistant', text: 'first reply\nINTERRUPT: blob\ntrace: noise' },
      { role: 'user', text: 'second prompt' },
      { role: 'assistant', text: 'second reply' },
    ];
    expect(transcriptMarkdown(messages)).toBe(
      [
        '# Chat transcript',
        '',
        '## You',
        '',
        'first prompt',
        '',
        '## Assistant',
        '',
        'first reply',
        '',
        '## You',
        '',
        'second prompt',
        '',
        '## Assistant',
        '',
        'second reply',
        '',
      ].join('\n'),
    );
  });

  it('exports an empty conversation as a bare header', () => {
    expect(transcriptMarkdown([])).toBe('# Chat transcript\n');
  });

  it('names the file chat-<date>.md', () => {
    expect(transcriptFilename(new Date('2026-11-04T12:00:00Z'))).toBe('chat-2026-11-04.md');
    expect(transcriptFilename()).toMatch(/^chat-\d{4}-\d{2}-\d{2}\.md$/);
  });
});

// Node has no window; the dictation suites pin and restore the global
// per test so capability detection is asserted, not assumed.
const dictationGlobals = globalThis as unknown as { window?: unknown };
const dictationSavedWindow = dictationGlobals.window;

function setDictationWindow(value: unknown) {
  dictationGlobals.window = value;
}

function fakeDictationCtor() {}

function dictationEvent(
  transcripts: Array<{ text: string; isFinal: boolean }>,
  resultIndex = 0,
): DictationResultEvent {
  return {
    resultIndex,
    results: transcripts.map((t) => ({ isFinal: t.isFinal, 0: { transcript: t.text } })),
  };
}

describe('voice dictation capability detection (issue #659)', () => {
  beforeEach(() => {
    setDictationWindow({});
  });

  afterEach(() => {
    dictationGlobals.window = dictationSavedWindow;
  });

  it('reports unsupported with the API globals removed', () => {
    setDictationWindow({});
    expect(dictationSupported()).toBe(false);
    expect(dictationCtor()).toBe(null);
  });

  it('ignores non-constructor globals', () => {
    setDictationWindow({ SpeechRecognition: 'nope', webkitSpeechRecognition: 42 });
    expect(dictationSupported()).toBe(false);
    expect(dictationCtor()).toBe(null);
  });

  it('prefers the standard constructor, webkit second', () => {
    function webkitFake() {}
    setDictationWindow({ SpeechRecognition: fakeDictationCtor, webkitSpeechRecognition: webkitFake });
    expect(dictationSupported()).toBe(true);
    expect(dictationCtor()).toBe(fakeDictationCtor);
    setDictationWindow({ webkitSpeechRecognition: webkitFake });
    expect(dictationSupported()).toBe(true);
    expect(dictationCtor()).toBe(webkitFake);
  });

  it('renders no button where the API is unavailable', () => {
    setDictationWindow({});
    expect(
      renderToStaticMarkup(
        <DictateButton recording={false} disabled={false} onToggle={() => {}} />,
      ),
    ).toBe('');
  });

  it('renders the mic button where the API exists', () => {
    setDictationWindow({ SpeechRecognition: fakeDictationCtor });
    const html = renderToStaticMarkup(
      <DictateButton recording={false} disabled={false} onToggle={() => {}} />,
    );
    expect(html).toContain('data-testid="chat-dictate"');
    expect(html).toContain('aria-label="Dictate with voice"');
    expect(html).toContain('aria-pressed="false"');
    expect(html).toContain('>Dictate</button>');
    expect(html).not.toContain('dictate-rec');
  });

  it('disables the mic button while streaming', () => {
    setDictationWindow({ SpeechRecognition: fakeDictationCtor });
    const html = renderToStaticMarkup(
      <DictateButton recording={false} disabled onToggle={() => {}} />,
    );
    expect(html).toContain('data-testid="chat-dictate"');
    expect(html).toContain('disabled=""');
  });
});

describe('voice dictation toggle state (issue #659)', () => {
  beforeEach(() => {
    setDictationWindow({ SpeechRecognition: fakeDictationCtor });
  });

  afterEach(() => {
    dictationGlobals.window = dictationSavedWindow;
  });

  it('shows the recording state with a pulsing dot and pressed toggle', () => {
    const html = renderToStaticMarkup(
      <DictateButton recording disabled={false} onToggle={() => {}} />,
    );
    expect(html).toContain('data-testid="chat-dictate"');
    expect(html).toContain('aria-pressed="true"');
    expect(html).toContain('aria-label="Stop dictation"');
    expect(html).toContain('dictate-rec');
    expect(html).toContain('>Stop</button>');
    expect(html).not.toContain('>Dictate</button>');
  });
});

describe('voice dictation transcript append (issue #659)', () => {
  it('appends with a single separating space', () => {
    expect(appendDictationText('', 'hello')).toBe('hello');
    expect(appendDictationText('hi', 'there')).toBe('hi there');
    expect(appendDictationText('hi ', 'there')).toBe('hi there');
    expect(appendDictationText('line\n', 'there')).toBe('line\nthere');
  });

  it('ignores empty additions', () => {
    expect(appendDictationText('hi', '')).toBe('hi');
    expect(appendDictationText('hi', '   ')).toBe('hi');
    expect(appendDictationText('', '   ')).toBe('');
  });

  it('splits one event into final vs interim text', () => {
    expect(
      extractDictationText(
        dictationEvent([
          { text: 'hello ', isFinal: true },
          { text: 'wor', isFinal: false },
        ]),
      ),
    ).toEqual({ finalText: 'hello ', interimText: 'wor' });
  });

  it('reads only results new since resultIndex', () => {
    expect(
      extractDictationText(
        dictationEvent(
          [
            { text: 'already counted', isFinal: true },
            { text: 'new words', isFinal: true },
          ],
          1,
        ),
      ),
    ).toEqual({ finalText: 'new words', interimText: '' });
  });

  it('streams interim live, then replaces the preview with its final', () => {
    const prev = 'review the diff';
    const inserted = '';
    const preview = applyDictationResult(prev, inserted, '', 'and the tests');
    expect(preview.next).toBe('review the diff and the tests');
    const commit = applyDictationResult(preview.next, preview.inserted, 'and the tests', '');
    expect(commit.next).toBe('review the diff and the tests');
    const next = applyDictationResult(commit.next, commit.inserted, 'please', '');
    expect(next.next).toBe('review the diff and the tests please');
  });

  it('composes typed and dictated text in both orders', () => {
    const dictated = applyDictationResult('TODO', '', 'fix the flake', '');
    expect(dictated.next).toBe('TODO fix the flake');
    const typed = `${dictated.next}!`;
    const again = applyDictationResult(typed, dictated.inserted, 'today', '');
    expect(again.next).toBe('TODO fix the flake! today');
  });

  it('starts a fresh session without stripping composer text', () => {
    // Final-only events track no interim suffix: nothing to rebase.
    expect(applyDictationResult('keep me', '', 'new words', '')).toEqual({
      next: 'keep me new words',
      inserted: '',
    });
  });

  it('accumulates finals across events without re-stripping them', () => {
    const first = applyDictationResult('', '', 'first', 'second…');
    expect(first).toEqual({ next: 'first second…', inserted: ' second…' });
    const second = applyDictationResult(first.next, first.inserted, 'second', '');
    expect(second).toEqual({ next: 'first second', inserted: '' });
    const third = applyDictationResult(second.next, second.inserted, 'third', '');
    expect(third).toEqual({ next: 'first second third', inserted: '' });
  });

  it('leaves the composer untouched on an empty event', () => {
    expect(applyDictationResult('hi', '', '', '')).toEqual({ next: 'hi', inserted: '' });
  });
});

describe('voice dictation error hints (issue #659)', () => {
  it('names the browser permission path on mic denial', () => {
    const hint = dictationErrorHint('not-allowed') ?? '';
    expect(hint).toContain('browser site settings');
    expect(dictationErrorHint('service-not-allowed')).toBe(hint);
  });

  it('maps no-speech, network, and audio-capture to one-liners', () => {
    expect(dictationErrorHint('no-speech')).toContain('No speech heard');
    expect(dictationErrorHint('network')).toContain('network connection');
    expect(dictationErrorHint('audio-capture')).toContain('No microphone found');
  });

  it('stays silent on tap-to-stop and unknown codes', () => {
    expect(dictationErrorHint('aborted')).toBe(null);
    expect(dictationErrorHint('')).toBe(null);
    expect(dictationErrorHint('bogus')).toBe(null);
  });

  it('renders the hint as a status line', () => {
    const html = renderToStaticMarkup(<DictationHint text="No speech heard — tap Dictate and try again." />);
    expect(html).toContain('data-testid="chat-dictate-hint"');
    expect(html).toContain('role="status"');
    expect(html).toContain('No speech heard');
  });
});

describe('live conversation persistence (issue #573)', () => {
  const noop = () => {};

  function stubSessionStorage(seed: Record<string, string> = {}) {
    const store = new Map<string, string>(Object.entries(seed));
    vi.stubGlobal('sessionStorage', {
      getItem: (k: string) => (store.has(k) ? (store.get(k) as string) : null),
      setItem: (k: string, v: string) => {
        store.set(k, v);
      },
      removeItem: (k: string) => {
        store.delete(k);
      },
    });
    return store;
  }

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('round-trips the exchange through sessionStorage under the chat key', () => {
    const store = stubSessionStorage();
    const messages: ChatMessage[] = [
      { role: 'user', text: 'ping' },
      { role: 'assistant', text: 'pong' },
    ];
    saveChatMessages(messages);
    expect(store.get(CHAT_MESSAGES_KEY)).toBe(JSON.stringify(messages));
    expect(loadChatMessages()).toEqual(messages);
  });

  it('restores the conversation on mount instead of the empty prompt', () => {
    stubSessionStorage({
      [CHAT_MESSAGES_KEY]: JSON.stringify([
        { role: 'user', text: 'ping' },
        { role: 'assistant', text: 'pong' },
      ]),
    });
    const html = renderToStaticMarkup(<ChatView repo="r" seed={null} onSeedUsed={noop} />);
    expect(html).toContain('data-testid="chat-msg-user-0"');
    expect(html).toContain('ping');
    expect(html).toContain('pong');
    expect(html).not.toContain('data-testid="chat-empty"');
  });

  it('starts empty with no snapshot stored', () => {
    stubSessionStorage();
    const html = renderToStaticMarkup(<ChatView repo="r" seed={null} onSeedUsed={noop} />);
    expect(html).toContain('data-testid="chat-empty"');
    expect(html).toContain('Ask anything about this repo.');
  });

  it('starts empty on a corrupt or misshapen snapshot', () => {
    stubSessionStorage({ [CHAT_MESSAGES_KEY]: 'not-json{{' });
    expect(loadChatMessages()).toEqual([]);
    stubSessionStorage({ [CHAT_MESSAGES_KEY]: JSON.stringify({ role: 'user' }) });
    expect(loadChatMessages()).toEqual([]);
    stubSessionStorage({
      [CHAT_MESSAGES_KEY]: JSON.stringify([
        { role: 'user', text: 'kept' },
        { role: 'system', text: 'dropped' },
        { role: 'assistant' },
        null,
      ]),
    });
    expect(loadChatMessages()).toEqual([{ role: 'user', text: 'kept' }]);
  });

  it('validates stored entries by role and text', () => {
    expect(isChatMessage({ role: 'user', text: 'hi' })).toBe(true);
    expect(isChatMessage({ role: 'assistant', text: '' })).toBe(true);
    expect(isChatMessage({ role: 'system', text: 'hi' })).toBe(false);
    expect(isChatMessage({ role: 'user' })).toBe(false);
    expect(isChatMessage(null)).toBe(false);
    expect(isChatMessage('hi')).toBe(false);
  });

  it('trims the trailing typing placeholder from snapshots only', () => {
    const live: ChatMessage[] = [
      { role: 'user', text: 'ping' },
      { role: 'assistant', text: '' },
    ];
    expect(settledChatMessages(live)).toEqual([{ role: 'user', text: 'ping' }]);
    // The live state keeps the placeholder for the typing indicator.
    expect(live).toHaveLength(2);
    const settled: ChatMessage[] = [
      { role: 'user', text: 'ping' },
      { role: 'assistant', text: 'pong' },
    ];
    expect(settledChatMessages(settled)).toBe(settled);
    expect(settledChatMessages([])).toEqual([]);
  });

  it('persists the cleared (empty) conversation, resetting the store', () => {
    const store = stubSessionStorage({
      [CHAT_MESSAGES_KEY]: JSON.stringify([{ role: 'user', text: 'ping' }]),
    });
    saveChatMessages(settledChatMessages([]));
    expect(store.get(CHAT_MESSAGES_KEY)).toBe('[]');
    expect(loadChatMessages()).toEqual([]);
  });
});

describe('stream error parsing (issue #466)', () => {
  function sseReader(frames: string[]): ReadableStreamDefaultReader<Uint8Array> {
    const enc = new TextEncoder();
    const queue = frames.map((f) => enc.encode(f));
    return {
      read: async () => {
        const value = queue.shift();
        if (!value) return { done: true, value: undefined };
        return { done: false, value };
      },
    } as unknown as ReadableStreamDefaultReader<Uint8Array>;
  }

  function pump(frames: string[]) {
    return pumpStream(sseReader(frames), () => {}, () => {}, () => {}, () => {}, () => {});
  }

  it('throws the human message, not the JSON envelope', async () => {
    await expect(pump(['event: error\ndata: {"error":"unknown repo"}\n\n'])).rejects.toThrow(
      'unknown repo',
    );
    await expect(pump(['event: error\ndata: {"error":"unknown repo"}\n\n'])).rejects.toThrow(
      /^unknown repo$/,
    );
  });

  it('falls back to raw data when the body is not JSON', async () => {
    await expect(pump(['event: error\ndata: boom\n\n'])).rejects.toThrow(/^boom$/);
  });

  it('falls back to raw data when the JSON has no message', async () => {
    await expect(pump(['event: error\ndata: {}\n\n'])).rejects.toThrow(/^\{\}$/);
    await expect(pump(['event: error\ndata: {"error":""}\n\n'])).rejects.toThrow(
      /^\{"error":""\}$/,
    );
  });
});

describe('conversation thread continuity (issue #467)', () => {
  function stubSessionStorage(seed: Record<string, string> = {}) {
    const store = new Map<string, string>(Object.entries(seed));
    vi.stubGlobal('sessionStorage', {
      getItem: (k: string) => (store.has(k) ? (store.get(k) as string) : null),
      setItem: (k: string, v: string) => {
        store.set(k, v);
      },
      removeItem: (k: string) => {
        store.delete(k);
      },
    });
    return store;
  }

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('reuses the live id across sends without minting', () => {
    const mint = vi.fn(() => 'fresh-id');
    expect(resolveChatSessionId('live-id', 'stored-id', mint)).toBe('live-id');
    expect(mint).not.toHaveBeenCalled();
  });

  it('restores the stored snapshot when no live id exists', () => {
    const mint = vi.fn(() => 'fresh-id');
    expect(resolveChatSessionId(null, 'stored-id', mint)).toBe('stored-id');
    expect(mint).not.toHaveBeenCalled();
  });

  it('mints only when neither a live id nor a snapshot exists', () => {
    const mint = vi.fn(() => 'fresh-id');
    expect(resolveChatSessionId(null, null, mint)).toBe('fresh-id');
    expect(mint).toHaveBeenCalledTimes(1);
  });

  it('treats empty ids as absent, falling through to mint', () => {
    const mint = vi.fn(() => 'fresh-id');
    expect(resolveChatSessionId('', '', mint)).toBe('fresh-id');
    expect(resolveChatSessionId('', 'stored-id', mint)).toBe('stored-id');
    expect(mint).toHaveBeenCalledTimes(1);
  });

  it('round-trips the thread id through sessionStorage under the session key', () => {
    const store = stubSessionStorage();
    saveChatSessionId('thread-abc');
    expect(store.get(CHAT_SESSION_KEY)).toBe('thread-abc');
    expect(loadChatSessionId()).toBe('thread-abc');
  });

  it('loads null when no snapshot is stored', () => {
    stubSessionStorage();
    expect(loadChatSessionId()).toBe(null);
    stubSessionStorage({ [CHAT_SESSION_KEY]: '' });
    expect(loadChatSessionId()).toBe(null);
  });

  it('clears the snapshot on new chat so the next send mints fresh', () => {
    const store = stubSessionStorage({ [CHAT_SESSION_KEY]: 'thread-abc' });
    clearChatSessionId();
    expect(store.has(CHAT_SESSION_KEY)).toBe(false);
    expect(loadChatSessionId()).toBe(null);
  });
});

describe('seed staging fills, never sends (issue #507)', () => {
  const noop = () => {};

  function stubSessionStorage(seed: Record<string, string> = {}) {
    const store = new Map<string, string>(Object.entries(seed));
    vi.stubGlobal('sessionStorage', {
      getItem: (k: string) => (store.has(k) ? (store.get(k) as string) : null),
      setItem: (k: string, v: string) => {
        store.set(k, v);
      },
      removeItem: (k: string) => {
        store.delete(k);
      },
    });
    return store;
  }

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function composerText(html: string): string | null {
    const m = html.match(
      /<textarea[^>]*data-testid="chat-prompt"[^>]*>([\s\S]*?)<\/textarea>/,
    );
    return m ? (m[1] as string) : null;
  }

  function sendTag(html: string): string {
    return html.match(/<button[^>]*data-testid="chat-send"[^>]*>/)?.[0] ?? '';
  }

  it('stages a fresh seed for review, ignoring missing or restaged seeds', () => {
    expect(stageSeed('What should I work on next?', false)).toBe(
      'What should I work on next?',
    );
    expect(stageSeed(null, false)).toBeNull();
    expect(stageSeed('', false)).toBeNull();
    expect(stageSeed('Plan my day', true)).toBeNull();
  });

  it('lands the seed in the composer without starting a turn', () => {
    stubSessionStorage();
    const html = renderToStaticMarkup(
      <ChatView repo="r" seed="What should I work on next?" onSeedUsed={noop} />,
    );
    expect(composerText(html)).toBe('What should I work on next?');
    expect(html).toContain('data-testid="chat-empty"');
    expect(html).not.toContain('data-testid="chat-progress"');
    expect(html).not.toContain('chat-msg-user-');
  });

  it('leaves explicit Send enabled so the turn starts deliberately', () => {
    stubSessionStorage();
    const seeded = renderToStaticMarkup(
      <ChatView repo="r" seed="What should I work on next?" onSeedUsed={noop} />,
    );
    expect(sendTag(seeded)).not.toContain('disabled');
    const empty = renderToStaticMarkup(
      <ChatView repo="r" seed={null} onSeedUsed={noop} />,
    );
    expect(composerText(empty)).toBe('');
    expect(sendTag(empty)).toContain('disabled');
  });
});

describe('chat draft autosave (issue #402)', () => {
  const noop = () => {};

  function stubSessionStorage(seed: Record<string, string> = {}) {
    const store = new Map<string, string>(Object.entries(seed));
    vi.stubGlobal('sessionStorage', {
      getItem: (k: string) => (store.has(k) ? (store.get(k) as string) : null),
      setItem: (k: string, v: string) => {
        store.set(k, v);
      },
      removeItem: (k: string) => {
        store.delete(k);
      },
    });
    return store;
  }

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function composerText(html: string): string | null {
    const m = html.match(
      /<textarea[^>]*data-testid="chat-prompt"[^>]*>([\s\S]*?)<\/textarea>/,
    );
    return m ? (m[1] as string) : null;
  }

  it('round-trips the draft through sessionStorage under the draft key', () => {
    const store = stubSessionStorage();
    saveChatDraft('half-typed prompt');
    expect(store.get(CHAT_DRAFT_KEY)).toBe('half-typed prompt');
    expect(loadChatDraft()).toBe('half-typed prompt');
  });

  it('restores the draft in the composer on mount', () => {
    stubSessionStorage({ [CHAT_DRAFT_KEY]: 'half-typed prompt' });
    const html = renderToStaticMarkup(
      <ChatView repo="r" seed={null} onSeedUsed={noop} />,
    );
    expect(composerText(html)).toBe('half-typed prompt');
  });

  it('starts with an empty composer when no draft is stored', () => {
    stubSessionStorage();
    expect(loadChatDraft()).toBe('');
    const html = renderToStaticMarkup(
      <ChatView repo="r" seed={null} onSeedUsed={noop} />,
    );
    expect(composerText(html)).toBe('');
  });

  it('prefers an explicit seed over a stored draft', () => {
    stubSessionStorage({ [CHAT_DRAFT_KEY]: 'stale draft' });
    const html = renderToStaticMarkup(
      <ChatView repo="r" seed="What should I work on next?" onSeedUsed={noop} />,
    );
    expect(composerText(html)).toBe('What should I work on next?');
  });

  it('clears the snapshot on send so the draft is gone after', () => {
    const store = stubSessionStorage({ [CHAT_DRAFT_KEY]: 'sent prompt' });
    clearChatDraft();
    expect(store.has(CHAT_DRAFT_KEY)).toBe(false);
    expect(loadChatDraft()).toBe('');
  });

  it('treats missing sessionStorage as empty without throwing', () => {
    vi.stubGlobal('sessionStorage', undefined);
    expect(loadChatDraft()).toBe('');
    expect(() => saveChatDraft('x')).not.toThrow();
    expect(() => clearChatDraft()).not.toThrow();
  });

  it('survives storage failures without throwing', () => {
    vi.stubGlobal('sessionStorage', {
      getItem: () => {
        throw new Error('denied');
      },
      setItem: () => {
        throw new Error('denied');
      },
      removeItem: () => {
        throw new Error('denied');
      },
    });
    expect(loadChatDraft()).toBe('');
    expect(() => saveChatDraft('x')).not.toThrow();
    expect(() => clearChatDraft()).not.toThrow();
  });
});

describe('saved prompt templates (issue #807)', () => {
  function stubLocalStorage(seed: Record<string, string> = {}) {
    const store = new Map<string, string>(Object.entries(seed));
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => (store.has(k) ? (store.get(k) as string) : null),
      setItem: (k: string, v: string) => {
        store.set(k, v);
      },
      removeItem: (k: string) => {
        store.delete(k);
      },
    });
    return store;
  }

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('seeds three starters demonstrating the text slot', () => {
    expect(SEED_TEMPLATES).toHaveLength(3);
    for (const t of SEED_TEMPLATES) {
      expect(t.id.length).toBeGreaterThan(0);
      expect(t.label.length).toBeGreaterThan(0);
      expect(t.text).toContain(TEMPLATE_TEXT_SLOT);
    }
    expect(new Set(SEED_TEMPLATES.map((t) => t.id)).size).toBe(3);
  });

  it('loads seeds on first run, stored templates after', () => {
    stubLocalStorage();
    expect(loadPromptTemplates()).toEqual([...SEED_TEMPLATES]);
    const mine = [{ id: 't1', label: 'Mine', text: 'hello' }];
    stubLocalStorage({ [CHAT_TEMPLATES_KEY]: JSON.stringify(mine) });
    expect(loadPromptTemplates()).toEqual(mine);
  });

  it('heals corrupt or misshapen stores to seeds, keeps an emptied store empty', () => {
    stubLocalStorage({ [CHAT_TEMPLATES_KEY]: 'not-json{{' });
    expect(loadPromptTemplates()).toEqual([...SEED_TEMPLATES]);
    stubLocalStorage({ [CHAT_TEMPLATES_KEY]: JSON.stringify({ id: 'x' }) });
    expect(loadPromptTemplates()).toEqual([...SEED_TEMPLATES]);
    stubLocalStorage({ [CHAT_TEMPLATES_KEY]: JSON.stringify([{ id: 't1' }]) });
    expect(loadPromptTemplates()).toEqual([]);
    stubLocalStorage({ [CHAT_TEMPLATES_KEY]: JSON.stringify([]) });
    expect(loadPromptTemplates()).toEqual([]);
  });

  it('round-trips templates through localStorage under the templates key', () => {
    const store = stubLocalStorage();
    const templates = [
      ...SEED_TEMPLATES,
      { id: 't9', label: 'Deploy', text: 'deploy now' },
    ];
    savePromptTemplates(templates);
    expect(store.get(CHAT_TEMPLATES_KEY)).toBe(JSON.stringify(templates));
    expect(loadPromptTemplates()).toEqual(templates);
  });

  it('treats missing localStorage as seeds without throwing', () => {
    vi.stubGlobal('localStorage', undefined);
    expect(loadPromptTemplates()).toEqual([...SEED_TEMPLATES]);
    expect(() => savePromptTemplates([])).not.toThrow();
  });

  it('derives the label from the first draft line', () => {
    expect(templateLabelForDraft('hello world')).toBe('hello world');
    expect(templateLabelForDraft('  first line\nsecond line  ')).toBe('first line');
    expect(templateLabelForDraft('   ')).toBeNull();
    expect(templateLabelForDraft('')).toBeNull();
    const label = templateLabelForDraft('x'.repeat(100));
    expect(label?.length).toBe(40);
    expect(label?.endsWith('…')).toBe(true);
  });

  it('makes a template from a non-blank draft only', () => {
    expect(makeTemplateFromDraft('   ')).toBeNull();
    const made = makeTemplateFromDraft('Ship it\nsecond line');
    expect(made).not.toBeNull();
    expect(made?.label).toBe('Ship it');
    expect(made?.text).toBe('Ship it\nsecond line');
    expect(made?.id).toMatch(/^t[0-9a-z]+$/);
    expect(makeTemplateFromDraft('one')?.id).not.toBe(makeTemplateFromDraft('one')?.id);
  });

  it('inserts at the caret, past the end when no slot', () => {
    expect(insertTemplateAt('', 0, 'hi')).toEqual({ text: 'hi', caret: 2 });
    expect(insertTemplateAt('ab', 1, 'X')).toEqual({ text: 'aXb', caret: 2 });
    expect(insertTemplateAt('ab', 99, 'X')).toEqual({ text: 'abX', caret: 3 });
    expect(insertTemplateAt('ab', -4, 'X')).toEqual({ text: 'Xab', caret: 1 });
  });

  it('consumes text slots and lands the caret at the first', () => {
    expect(insertTemplateAt('', 0, 'Explain {text} now')).toEqual({
      text: 'Explain  now',
      caret: 8,
    });
    expect(insertTemplateAt('> ', 2, 'A {text} B {text}')).toEqual({
      text: '> A  B ',
      caret: 4,
    });
  });

  it('renders the panel with save, insert, and delete affordances', () => {
    const noop = () => {};
    const html = renderToStaticMarkup(
      <TemplatePanel
        templates={[
          { id: 'standup', label: 'Standup', text: 'sum now' },
          { id: 't9', label: 'Deploy', text: 'deploy' },
        ]}
        canSave
        disabled={false}
        onSave={noop}
        onInsert={noop}
        onDelete={noop}
      />,
    );
    expect(html).toContain('data-testid="chat-templates-panel"');
    expect(html).toContain('data-testid="chat-template-save"');
    expect(html).toContain('data-testid="chat-template-insert-standup"');
    expect(html).toContain('data-testid="chat-template-delete-t9"');
    expect(html).toContain('Insert template: Standup');
    expect(html).toContain('Delete template: Deploy');
    expect(html).toContain('role="group"');
  });

  it('disables save on an empty draft and everything while streaming', () => {
    const noop = () => {};
    const empty = renderToStaticMarkup(
      <TemplatePanel
        templates={[]}
        canSave={false}
        disabled={false}
        onSave={noop}
        onInsert={noop}
        onDelete={noop}
      />,
    );
    expect(empty).toContain('disabled');
    expect(empty).toContain('No templates yet');
    const busy = renderToStaticMarkup(
      <TemplatePanel
        templates={[{ id: 't1', label: 'A', text: 'a' }]}
        canSave
        disabled
        onSave={noop}
        onInsert={noop}
        onDelete={noop}
      />,
    );
    // save + insert + delete all disabled
    expect(busy.match(/disabled/g)?.length).toBe(3);
  });

  it('shows the templates toggle in the composer actions', () => {
    const noop = () => {};
    const html = renderToStaticMarkup(<ChatView repo="r" seed={null} onSeedUsed={noop} />);
    expect(html).toContain('data-testid="chat-templates"');
    expect(html).toContain('aria-expanded="false"');
    expect(html).not.toContain('data-testid="chat-templates-panel"');
  });
});

describe('split console layout (issue #839)', () => {
  const noop = () => {};

  function stubSessionStorage(seed: Record<string, string> = {}) {
    const store = new Map<string, string>(Object.entries(seed));
    vi.stubGlobal('sessionStorage', {
      getItem: (k: string) => (store.has(k) ? (store.get(k) as string) : null),
      setItem: (k: string, v: string) => {
        store.set(k, v);
      },
      removeItem: (k: string) => {
        store.delete(k);
      },
    });
    return store;
  }

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('splits the conversation canvas from the runs/agent rail', () => {
    stubSessionStorage();
    const html = renderToStaticMarkup(<ChatView repo="r" seed={null} onSeedUsed={noop} />);
    expect(html).toContain('chat-main');
    expect(html).toContain('chat-side');
    expect(html).toContain('data-testid="chat-backend-picker"');
    expect(html).toContain('data-testid="chat-send"');
  });

  it('shows prompt starters only before the first thread exists', () => {
    stubSessionStorage();
    const fresh = renderToStaticMarkup(<ChatView repo="r" seed={null} onSeedUsed={noop} />);
    expect(fresh).toContain('data-testid="chat-prompt-chips"');
    stubSessionStorage({
      [CHAT_MESSAGES_KEY]: JSON.stringify([
        { role: 'user', text: 'hi' },
        { role: 'assistant', text: 'yo' },
      ]),
    });
    const threaded = renderToStaticMarkup(<ChatView repo="r" seed={null} onSeedUsed={noop} />);
    expect(threaded).toContain('data-testid="chat-history"');
    expect(threaded).not.toContain('data-testid="chat-prompt-chips"');
  });
});

describe('repo metrics strip (issue #897)', () => {
  const metrics = {
    repo: 'r',
    branch: 'main',
    clean: false,
    status_count: 3,
    todo_count: 12,
    todo_hits: [],
    diff_stat: ' a.py | 2 +-\n 1 file changed\n',
    diff_files: 1,
  };

  it('formats the one-line strip', () => {
    expect(repoMetricsLine(metrics)).toBe('3 uncommitted · 12 TODOs · 1 file changed');
    expect(repoMetricsLine({ ...metrics, status_count: 0, todo_count: 0, diff_files: 0 })).toBe(
      'Clean · 0 TODOs · no diff',
    );
    expect(repoMetricsLine({ ...metrics, todo_count: 1 })).toContain('1 TODO ·');
  });

  it('renders the strip with the diff stat as hover detail', () => {
    const html = renderToStaticMarkup(<RepoMetricsStrip metrics={metrics} />);
    expect(html).toContain('data-testid="chat-metrics-strip"');
    expect(html).toContain('3 uncommitted · 12 TODOs · 1 file changed');
    expect(html).toContain('title="');
    expect(html).toContain('a.py');
  });
});

describe('composer control grouping (issue #897)', () => {
  it('groups session utilities behind Send with pills first', () => {
    const noop = () => {};
    // The dictate button only renders where the SpeechRecognition API
    // exists; enable it so the full utility row is asserted.
    setDictationWindow({
      SpeechRecognition: fakeDictationCtor,
      location: { search: '' },
      localStorage: {
        getItem: () => null,
        setItem: () => {},
        removeItem: () => {},
      },
    });
    let html: string;
    try {
      html = renderToStaticMarkup(<ChatView repo="r" seed={null} onSeedUsed={noop} />);
    } finally {
      dictationGlobals.window = dictationSavedWindow;
    }
    const utilsIdx = html.indexOf('data-testid="chat-utils"');
    expect(utilsIdx).toBeGreaterThan(0);
    const tail = html.slice(utilsIdx);
    expect(tail).toContain('data-testid="chat-dictate"');
    expect(tail).toContain('data-testid="chat-clear"');
    expect(tail).toContain('data-testid="chat-export"');
    expect(tail).toContain('data-testid="chat-templates"');
    expect(html.indexOf('data-testid="chat-backend-picker"')).toBeLessThan(utilsIdx);
    expect(html.indexOf('data-testid="chat-send"')).toBeLessThan(utilsIdx);
  });
});

describe('rail modules (issue #897)', () => {
  it('past runs renders as a distinct module', () => {
    expect(paneHtml([pastRun()])).toContain('class="chat-module"');
  });

  it('single continue/delete control per run (no duplicates)', () => {
    const html = paneHtml([pastRun(), pastRun({ id: 'run-1' })]);
    for (const id of ['run-0', 'run-1']) {
      expect(html.split(`data-testid="chat-run-delete-${id}"`).length - 1).toBe(1);
      expect(html.split(`data-testid="chat-run-continue-${id}"`).length - 1).toBe(1);
    }
  });
});

describe('chat actionable rows (issue #907)', () => {
  const metrics = {
    repo: 'demo',
    branch: 'main',
    clean: false,
    status_count: 3,
    todo_count: 0,
    todo_hits: [],
    diff_stat: ' a.py | 2 +-\n',
    diff_files: 1,
  };

  it('opens the diff modal from a dirty metrics strip', () => {
    const html = renderToStaticMarkup(
      <RepoMetricsStrip metrics={metrics} onOpenDiff={() => {}} />,
    );
    expect(html).toContain('data-testid="chat-metrics-diff"');
    expect(html).toContain('View diff');
  });

  it('hides the diff action on a clean tree', () => {
    const html = renderToStaticMarkup(
      <RepoMetricsStrip
        metrics={{ ...metrics, clean: true, status_count: 0, diff_files: 0, diff_stat: '' }}
        onOpenDiff={() => {}}
      />,
    );
    expect(html).not.toContain('chat-metrics-diff');
  });

  it('offers copy on run traces', () => {
    const html = renderToStaticMarkup(
      <TraceTimeline
        trace={[{ type: 'run_start', ts: 100, script: 's', thread: 't', model: 'm' }] as never}
        t0={100}
        narrow={false}
      />,
    );
    expect(html).toContain('data-testid="chat-trace-copy"');
    expect(html).toContain('Copy trace');
  });
});

describe('past runs repo scope (issue #933)', () => {
  it('shortens owner/name tags, passes nulls through', () => {
    expect(shortRunRepo('arii/remote-server')).toBe('remote-server');
    expect(shortRunRepo('remote-server')).toBe('remote-server');
    expect(shortRunRepo(null)).toBe(null);
    expect(shortRunRepo(undefined)).toBe(null);
    expect(shortRunRepo('')).toBe(null);
    expect(shortRunRepo('arii/')).toBe(null);
  });

  it('matches the selected repo, keeps unattributed rows visible', () => {
    expect(runMatchesRepo(pastRun({ repo: 'arii/r' }), 'r')).toBe(true);
    expect(runMatchesRepo(pastRun({ repo: 'arii/other' }), 'r')).toBe(false);
    expect(runMatchesRepo(pastRun({ repo: null }), 'r')).toBe(true);
  });

  it('filters to the selected repo unless widened', () => {
    const runs = [
      pastRun({ id: 'run-0', repo: 'arii/r' }),
      pastRun({ id: 'run-1', repo: 'arii/other' }),
      pastRun({ id: 'run-2', repo: null }),
    ];
    expect(filterRunsByRepo(runs, 'r', false).map((r) => r.id)).toEqual([
      'run-0',
      'run-2',
    ]);
    expect(filterRunsByRepo(runs, 'r', true)).toBe(runs);
  });

  it('badges each tagged run, skips the badge when unattributed', () => {
    const html = paneHtml(
      [pastRun({ repo: 'arii/r' }), pastRun({ id: 'run-1', repo: null })],
      null,
      null,
      'r',
      true,
    );
    expect(html).toContain('data-testid="chat-run-repo-run-0"');
    expect(html).toContain('>r</span>');
    expect(html).not.toContain('data-testid="chat-run-repo-run-1"');
  });

  it('scopes to the repo by default with a widen toggle and note', () => {
    const html = paneHtml(
      [pastRun({ repo: 'arii/r' }), pastRun({ id: 'run-1', repo: 'arii/other' })],
      null,
      null,
      'r',
      false,
    );
    expect(html).toContain('data-testid="chat-run-run-0"');
    expect(html).not.toContain('data-testid="chat-run-run-1"');
    expect(html).toContain('data-testid="chat-runs-scope"');
    expect(html).toContain('aria-pressed="false"');
    expect(html).toContain('data-testid="chat-runs-scope-note"');
    expect(html).toContain('Scoped to r · 1 other-repo run hidden');
  });

  it('widened view shows every run with the toggle pressed', () => {
    const html = paneHtml(
      [pastRun({ repo: 'arii/r' }), pastRun({ id: 'run-1', repo: 'arii/other' })],
      null,
      null,
      'r',
      true,
    );
    expect(html).toContain('data-testid="chat-run-run-0"');
    expect(html).toContain('data-testid="chat-run-run-1"');
    expect(html).toContain('aria-pressed="true"');
    expect(html).not.toContain('data-testid="chat-runs-scope-note"');
  });

  it('empty scope keeps the toggle with an empty note, not a blank pane', () => {
    const html = paneHtml([pastRun({ repo: 'arii/other' })], null, null, 'r', false);
    expect(html).toContain('data-testid="chat-runs"');
    expect(html).toContain('data-testid="chat-runs-scope"');
    expect(html).toContain('data-testid="chat-runs-empty-scope"');
    expect(html).toContain('No runs for r yet.');
  });

  it('metered total covers the visible runs only', () => {
    const html = paneHtml(
      [
        pastRun({ repo: 'arii/r', metered: true, cost_usd: 0.01 }),
        pastRun({ id: 'run-1', repo: 'arii/other', metered: true, cost_usd: 5 }),
      ],
      null,
      null,
      'r',
      false,
    );
    expect(html).toContain('Metered total $0.01');
  });
});

describe('composer auto route line (issue #933)', () => {
  const noop = () => {};

  it('reads at full weight in Auto mode', () => {
    const html = renderToStaticMarkup(<ChatView repo="r" seed={null} onSeedUsed={noop} />);
    expect(html).toContain('data-testid="chat-route-hint"');
    expect(html).toContain('class="chat-route-auto"');
  });
});

describe('pressed toggle fill (issue #1183)', () => {
  const noop = () => {};
  // Stylesheet under test: these suites render to static markup
  // without a DOM, so the rule text is asserted directly (vitest
  // empties ?raw CSS imports in this repo).
  const indexCss = readFileSync(fileURLToPath(new URL('../index.css', import.meta.url)), 'utf8');

  function pickerHtml(): string {
    const html = renderToStaticMarkup(<ChatView repo="r" seed={null} onSeedUsed={noop} />);
    const start = html.indexOf('data-testid="chat-backend-picker"');
    expect(start).toBeGreaterThanOrEqual(0);
    const end = html.indexOf('data-testid="chat-send"');
    expect(end).toBeGreaterThan(start);
    return html.slice(start, end);
  }

  it('backend picker exposes one pressed toggle per option', () => {
    // The generic pressed-fill rule keys off aria-pressed alone, so
    // every option must carry the attribute — exactly one true.
    for (const o of CHAT_BACKEND_OPTIONS) {
      expect(pickerHtml()).toContain(`data-testid="chat-backend-${o.value}"`);
    }
    const picker = pickerHtml();
    expect(picker.split('aria-pressed=').length - 1).toBe(CHAT_BACKEND_OPTIONS.length);
    expect(picker.split('aria-pressed="true"').length - 1).toBe(1);
  });

  it('default backend lights the Auto toggle', () => {
    const auto = pickerHtml().match(/data-testid="chat-backend-auto"[^>]*aria-pressed="true"/);
    expect(auto).not.toBeNull();
  });

  it('stylesheet gives every bare pressed toggle the accent fill', () => {
    // Generic, deliberately unscoped: pickers that only set
    // aria-pressed (backend, scope) inherit the pressed fill without
    // per-picker rules.
    const rule = indexCss.match(/^button\[aria-pressed="true"\] \{[^}]*\}/m);
    expect(rule).not.toBeNull();
    expect(rule![0]).toContain('background: var(--accent-solid)');
    expect(rule![0]).toContain('border-color: var(--accent-solid)');
    expect(rule![0]).toContain('color: #fff');
  });
});
