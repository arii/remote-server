import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import DangerZone from './DangerZone';

function render(runCount: number, finishedCount: number): string {
  return renderToStaticMarkup(
    <DangerZone
      mergeReady={null}
      finishedCount={finishedCount}
      runCount={runCount}
      onDangerAction={() => {}}
      onClear={() => {}}
      onTakeover={() => {}}
    />,
  );
}

function clearAllRow(html: string): string {
  const start = html.indexOf('data-testid="danger-clear-all"');
  expect(start).toBeGreaterThan(-1);
  return html.slice(start, html.indexOf('</li>', start));
}

describe('danger / system operations separation (issue #934)', () => {
  it('labels the section Danger / System Operations with disruptive copy', () => {
    const html = render(0, 0);
    expect(html).toContain('Danger / System Operations');
    expect(html).toContain('Disruptive');
    expect(html).toContain('data-testid="jobs-danger"');
    expect(html).toContain('aria-label="Danger / System Operations"');
  });

  it('contains only system ops, not agent workflows', () => {
    const html = render(2, 1);
    expect(html).toContain('data-testid="danger-deploy-buttons"');
    expect(html).toContain('Redeploy this console');
    expect(html).toContain('data-testid="danger-recreate-code"');
    expect(html).toContain('Restart code editor');
    expect(html).toContain('data-testid="danger-takeover"');
    expect(html).toContain('Take over terminal');
    // Write briefing plan is an agent workflow (#934) — not in danger.
    expect(html).not.toContain('data-testid="danger-create-plan"');
    expect(html).not.toContain('Write briefing plan');
    // Agent workflow launchers never appear in the danger section.
    expect(html).not.toContain('Repair worker');
    expect(html).not.toContain('Triage fanout');
    expect(html).not.toContain('UX audit');
  });

  it('guards every disruptive action with a confirmation', () => {
    const html = render(2, 1);
    // TapToFire marks confirm-backed triggers in the accessible name.
    expect(html).toContain('Redeploy this console (asks for confirmation)');
    expect(html).toContain('Restart code editor (asks for confirmation)');
    expect(html).toContain('Take over terminal (asks for confirmation)');
    expect(html).toContain('Merge approved PR (asks for confirmation)');
  });
});

describe('danger zone clear-all (issue #512)', () => {
  it('disables clear-all and says so while a run is active', () => {
    const row = clearAllRow(render(2, 1));
    expect(row).toContain('A run is still active');
    expect(row).toContain('disabled');
  });

  it('enables clear-all with honest copy once all runs finish', () => {
    const row = clearAllRow(render(2, 2));
    expect(row).toContain('Drops all runs.');
    expect(row).not.toContain('disabled');
    expect(row).not.toContain('will be killed');
  });

  it('disables clear-all when there is nothing to clear', () => {
    const row = clearAllRow(render(0, 0));
    expect(row).toContain('No runs to clear.');
    expect(row).toContain('disabled');
  });
});
