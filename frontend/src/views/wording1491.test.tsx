import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import type { AgentRun } from '../apiClient';
import { PlanRow } from './BriefingView';
import { RunsPane } from './ChatView';
import ReviewView, { IssueRowActions, ISSUE_AGENT_ACTIONS_HINT } from './ReviewView';

describe('Review More actions wording (#1491 finding 18)', () => {
  it('says the agent actions only fill the prompt', () => {
    const html = renderToStaticMarkup(
      <IssueRowActions
        repo="r"
        iss={{ number: 7, title: 't', url: 'u', state: 'open', labels: [] }}
        branch="main"
        onOpen={() => {}}
        onDefer={() => {}}
        onAskAgent={() => {}}
      />,
    );
    expect(html).toContain('data-testid="issue-more-hint-7"');
    expect(html).toContain(ISSUE_AGENT_ACTIONS_HINT);
    expect(ISSUE_AGENT_ACTIONS_HINT).toContain('Nothing runs until you send it');
  });
});

describe('Plans Execute wording (#1491 finding 24)', () => {
  it('explains that Execute fills the prompt', () => {
    const html = renderToStaticMarkup(
      <ul>
        <PlanRow repo="r" plan={{ name: 'p.md', size: 10 } as never} onAskAgent={() => {}} />
      </ul>,
    );
    expect(html).toContain('data-testid="briefing-plan-execute-hint"');
    expect(html).toContain('Nothing runs until you send it');
  });
});

describe('Review tab counts while loading (#1491 finding 16)', () => {
  it('shows no (0) counts before issues load', () => {
    const html = renderToStaticMarkup(
      <ReviewView repo="r" onAskAgent={() => {}} onSelectPr={() => {}} />,
    );
    expect(html).toContain('Loading issues…');
    expect(html).not.toContain('All (0)');
    expect(html).toMatch(/data-testid="issue-tab-all"[^>]*>All</);
  });
});

describe('Past runs scope button wording (#1491 finding 11)', () => {
  const run = {
    id: 'a', script: 's', model: 'm', provider: null, thread: 't', repo: 'r', started_ts: 1,
    duration_s: 1, ok: true, tools: [], tokens: 1, preview: '', prompt_first_line: '',
    has_prompt: true, cost_usd: null, metered: false,
  } as AgentRun;
  function title(showAll: boolean): string {
    const html = renderToStaticMarkup(
      <RunsPane
        runs={[run]} repo="r" showAllRepos={showAll} onToggleScope={() => {}} selected={null}
        detail={null} onSelect={() => {}} onContinue={() => {}} onDelete={() => {}}
        onClearFinished={() => {}} notice={null} budget={null}
      />,
    );
    const at = html.indexOf('data-testid="chat-runs-scope"');
    return html.slice(at, html.indexOf('>All repos<', at));
  }
  it('states the current scope before the action', () => {
    expect(title(true)).toContain('Showing runs from all repos. Press to show only r runs');
    expect(title(false)).toContain('Showing only r runs. Press to show runs from all repos');
  });
});

describe('Console naming (#1491 finding 1)', () => {
  it('page title and heading both say Buttons', () => {
    const root = fileURLToPath(new URL('../../', import.meta.url));
    expect(readFileSync(`${root}index.html`, 'utf8')).toContain('<title>Buttons · remote-server</title>');
    expect(readFileSync(`${root}src/App.tsx`, 'utf8')).toContain('Buttons operator console</h1>');
  });
});
