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
