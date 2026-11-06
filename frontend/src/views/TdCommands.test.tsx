import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import TdCommands from './TdCommands';

function render(): string {
  return renderToStaticMarkup(
    <TdCommands
      pr="12"
      onPrChange={() => {}}
      prStateCurrent={{ n: 12, ci: 'passing', conflicted: false }}
      prOk
      conflictBlock={null}
      onDispatch={() => {}}
    />,
  );
}

describe('Doctor/Preflight demotion (issue #1016)', () => {
  it('renders Doctor/Preflight inside a collapsed Advanced section', () => {
    const html = render();
    expect(html).toContain('data-testid="td-repo-group"');
    expect(html).toContain('Advanced (whole repo)');
    expect(html).toContain('data-testid="dispatch-td-doctor"');
    expect(html).toContain('data-testid="dispatch-td-preflight"');
    // Collapsed by default: details without the open attribute.
    expect(html).not.toContain('<details open');
    expect(html).toContain('rarely needed');
  });

  it('keeps the PR-side group front and center', () => {
    const html = render();
    expect(html).toContain('data-testid="td-pr-group"');
    expect(html).toContain('data-testid="dispatch-td-resolve-conflicts"');
  });
});
