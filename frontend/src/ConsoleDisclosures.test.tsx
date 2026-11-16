// @vitest-environment happy-dom
import { renderToStaticMarkup } from 'react-dom/server';
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import NowComposer from './components/NowComposer';
import { BriefingBody } from './views/BriefingView';
import JobsView from './views/JobsView';
import { QueueHeader } from './views/QueueView';
import TdCommands from './views/TdCommands';
import { loadBriefing, loadPrDetails } from './views/mockViews';
import { PrDetails } from './views/ReviewView';
import ChatView from './views/ChatView';
import type { AgentRun } from './apiClient';

vi.mock('./apiClient', async (original) => ({
  ...await original<typeof import('./apiClient')>(),
  fetchJobs: vi.fn().mockResolvedValue({ jobs: [] }),
}));
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Unconfigured test endpoint')));
});
afterEach(() => vi.unstubAllGlobals());

const noop = () => {};
function dom(html: string) {
  const host = document.createElement('div');
  host.innerHTML = html;
  return host;
}

describe('audit disclosures (#1317)', () => {
  it('keeps PR metadata and actions behind a concise collapsed summary', () => {
    const host = dom(renderToStaticMarkup(<BriefingBody
      data={{ ...loadBriefing(), prs: [{ number: 42, title: 'Fix checks', ci: 'passing', author: 'Ari', branch: 'fix-checks' }] }}
      ledger={[]} onAskAgent={noop} onOpenHistory={noop} onOpenReview={noop}
    />));
    const card = host.querySelector<HTMLDetailsElement>('[data-testid="briefing-pr-row"] > details')!;
    expect(card.open).toBe(false);
    expect(card.querySelector('summary')!.textContent).toBe('#42 Fix checks passing');
    expect(card.querySelector('summary')!.textContent).not.toContain('Ari');
    expect(card.textContent).toContain('Ari');
    for (const label of ['Plans', 'Next actions', 'Recent activity']) {
      const summary = [...host.querySelectorAll('summary')].find((s) => s.textContent === label)!;
      expect(summary).toBeDefined();
      expect((summary.parentElement as HTMLDetailsElement).open).toBe(false);
    }
  });

  it('uses workflow lines for exactly one failing-run count, not ledger failures', () => {
    const host = dom(renderToStaticMarkup(<BriefingBody
      data={{ ...loadBriefing(), workflow_lines: ['CI on main failed', 'Deploy on main failed'], failing_runs: [{}, {}, {}], narrative: null }}
      ledger={[{ kind: 'webhook', event: 'push', ok: false, ts: Date.now() / 1000 }]}
      onAskAgent={noop} onOpenHistory={noop} onOpenReview={noop}
    />));
    expect(host.textContent!.match(/2 failing runs/g)).toHaveLength(1);
    expect(host.querySelector('[data-testid="briefing-trend"]')).toBeNull();
    expect(host.textContent).not.toContain('1 failure.');
  });

  it('collapses workflows, schedules and danger operations without losing forms', async () => {
    const host = document.createElement('div');
    const root = createRoot(host);
    await act(async () => root.render(<JobsView repo="r" onAskAgent={noop} />));
    try {
      for (const label of ['Repair worker', 'Triage fanout', 'UX audit', 'Briefing plan', 'Agent commands', 'Scheduled runs', 'Danger / System Operations']) {
        const summary = [...host.querySelectorAll('summary')].find((s) => s.textContent === label)!;
        expect(summary).toBeDefined();
        expect((summary.parentElement as HTMLDetailsElement).open).toBe(false);
      }
      expect(host.querySelector('[data-testid="repair-dispatch"]')).not.toBeNull();
      expect(host.querySelector<HTMLInputElement>('[data-testid="td-pr"]')!.value).toBe('');
      expect(host.querySelector<HTMLInputElement>('[data-testid="td-pr"]')!.placeholder).toBe('PR number');
    } finally {
      await act(async () => root.unmount());
    }
  });

  it('discloses populated Past runs while retaining an idle composer', () => {
    const host = dom(renderToStaticMarkup(<ChatView repo="r" seed={null} onSeedUsed={noop}
      initialRuns={[{
        id: 'one', repo: 'r', has_prompt: false, script: 'deep_chat.py', model: 'm', provider: null,
        thread: 't', started_ts: 0, duration_s: 1, ok: true, tools: [], tokens: 0,
        preview: 'hello', prompt_first_line: '', cost_usd: 0, metered: false,
      } satisfies AgentRun]} />));
    const past = host.querySelector<HTMLDetailsElement>('[data-testid="chat-past-runs"]')!;
    expect(past.open).toBe(false);
    expect(past.querySelector('summary')!.textContent).toBe('Past runs (1)');
    expect(host.querySelector('[data-testid="chat-prompt"]')).not.toBeNull();
    expect(past.textContent).toContain('No stored prompt for this run.');
  });

  it('shows static disabled reasons adjacent to queue and PR-command controls', () => {
    const queue = dom(renderToStaticMarkup(<QueueHeader count={0} live dispatchable={[]} onRefresh={noop} onDispatchAll={noop} />));
    expect(queue.querySelector<HTMLButtonElement>('[data-testid="queue-dispatch-all"]')!.disabled).toBe(true);
    expect(queue.textContent).toContain('Nothing queued to dispatch.');
    const commands = dom(renderToStaticMarkup(<TdCommands pr="" prOk={false} prStateCurrent={null} conflictBlock={null} onPrChange={noop} onDispatch={noop} repo="r" />));
    expect(commands.textContent).toContain('Enter a valid PR number');
    expect(commands.textContent).toContain('Enter an issue number');
  });

  it('disables blank or blocked forms and enables a valid prompt', () => {
    const render = (prompt: string, blockedReason: string | null) => dom(renderToStaticMarkup(
      <NowComposer prompt={prompt} blockedReason={blockedReason} onChange={noop} onSend={noop} focusRequest={0} />,
    ));
    expect(render(' ', null).querySelector('button')!.disabled).toBe(true);
    expect(render('Investigate', null).querySelector('button')!.disabled).toBe(false);
    const blocked = render('Investigate', 'Choose a repository before sending.');
    expect(blocked.querySelector('button')!.disabled).toBe(true);
  });

  it('names the active merge instead of leaving a silently disabled detail action', () => {
    const details = loadPrDetails();
    const host = dom(renderToStaticMarkup(<PrDetails repo="r" details={details} mergeBlocked={null} mergeDisabled onMerge={noop} onAskAgent={noop} />));
    expect(host.textContent).toContain(`Merging PR #${details.number}…`);
  });

});
