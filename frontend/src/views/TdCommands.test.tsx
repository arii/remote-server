import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import TdCommands from './TdCommands';

function render(): string {
  return renderToStaticMarkup(
    <TdCommands
      pr=""
      onPrChange={() => {}}
      prStateCurrent={null}
      prOk={false}
      conflictBlock={null}
      onDispatch={() => {}}
      repo="remote-server"
    />,
  );
}

describe('one-tap Validate issue (issue #1024)', () => {
  it('renders the issue-side group next to the PR group', () => {
    const html = render();
    expect(html).toContain('data-testid="td-issue-group"');
    expect(html).toContain('data-testid="td-issue"');
    expect(html).toContain('data-testid="dispatch-td-validate-issue"');
    expect(html).toContain('Validate issue');
  });

  it('keeps Validate disabled until an issue number is typed', () => {
    const html = render();
    const start = html.indexOf('data-testid="dispatch-td-validate-issue"');
    expect(start).toBeGreaterThan(-1);
    expect(html.slice(start, start + 400)).toContain('disabled');
    // Read-only affordance: no verdict or error before any check runs.
    expect(html).not.toContain('data-testid="td-verify-verdict"');
    expect(html).not.toContain('data-testid="td-verify-error"');
  });

  it('does not disturb the PR-side group', () => {
    const html = render();
    expect(html).toContain('data-testid="td-pr-group"');
    // #1016 (PR #1049): whole-repo group removed with Doctor/Preflight.
    expect(html).not.toContain('data-testid="td-repo-group"');
  });
});
