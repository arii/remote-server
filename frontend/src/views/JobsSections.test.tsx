import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import DangerZone from './DangerZone';
import RepairSection from './RepairSection';
import TdCommands from './TdCommands';

describe('extracted jobs sections (issue #363)', () => {
  it('repair section renders picker and dispatch', () => {
    const html = renderToStaticMarkup(<RepairSection repo="remote-server" />);
    expect(html).toContain('data-testid="repair-section"');
    expect(html).toContain('data-testid="repair-picker"');
    expect(html).toContain('data-testid="repair-dispatch"');
    expect(html).toContain('data-testid="repair-status"');
  });

  it('td commands render pr and whole-repo groups', () => {
    const html = renderToStaticMarkup(
      <TdCommands
        pr="12"
        onPrChange={() => {}}
        prStateCurrent={{ n: 12, ci: 'passing', conflicted: false }}
        prOk
        fixCiBlock="CI is passing — nothing to fix."
        conflictBlock="No conflicts on this PR."
        onDispatch={() => {}}
      />,
    );
    expect(html).toContain('data-testid="td-pr-group"');
    expect(html).toContain('data-testid="td-repo-group"');
    expect(html).toContain('data-testid="td-pr"');
    expect(html).toContain('data-testid="dispatch-td-fix-ci"');
    expect(html).toContain('data-testid="dispatch-td-list-sessions"');
    expect(html).toContain('data-testid="dispatch-td-doctor"');
    expect(html).toContain('data-testid="dispatch-td-preflight"');
    expect(html).toContain('data-testid="dispatch-td-audit-pr"');
    expect(html).toContain('data-testid="dispatch-td-ai-review"');
    expect(html).toContain('data-testid="td-change-block"');
    expect(html).toContain('PR #12');
  });

  it('danger zone renders all rows with tap-to-fire actions', () => {
    const html = renderToStaticMarkup(
      <DangerZone
        mergeReady={2}
        finishedCount={1}
        runCount={3}
        onDangerAction={() => {}}
        onClear={() => {}}
        onTakeover={() => {}}
      />,
    );
    expect(html).toContain('data-testid="danger-merge-approved"');
    expect(html).toContain('data-testid="danger-clear-finished"');
    expect(html).toContain('data-testid="danger-clear-all"');
    expect(html).toContain('data-testid="danger-takeover"');
    expect(html).toContain('2 ready to merge.');
    expect(html).toContain('data-testid="tap-fire"');
  });

  it('danger zone reports zero merge-ready without the consequence line', () => {
    const html = renderToStaticMarkup(
      <DangerZone
        mergeReady={0}
        finishedCount={0}
        runCount={0}
        onDangerAction={() => {}}
        onClear={() => {}}
        onTakeover={() => {}}
      />,
    );
    expect(html).toContain('No approved, green PRs right now.');
    expect(html).toContain('Nothing finished to clear.');
    expect(html).toContain('No runs to clear.');
  });
});
