import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import type { WorkflowRun } from '../apiClient';
import { ContainerPills, RunRow } from './ObservabilitySection';

describe('run trace link (issue #584)', () => {
  const run = {
    id: 41760116751,
    name: 'CI',
    branch: 'fix/x',
    event: 'push',
    conclusion: 'failure',
    url: 'https://github.com/o/r/actions/runs/41760116751',
    diagnosed_cause: 'Tests failed.',
  } as WorkflowRun;

  it('opens the trace in a new tab, keeping the console tab', () => {
    const html = renderToStaticMarkup(<RunRow run={run} />);
    expect(html).toContain('Open run trace');
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noreferrer"');
    expect(html).toContain(run.url);
  });

  it('renders no link without a run url', () => {
    const html = renderToStaticMarkup(<RunRow run={{ ...run, url: '' }} />);
    expect(html).not.toContain('Open run trace');
    expect(html).not.toContain('<a ');
  });
});

describe('container list failure (issue #530)', () => {
  it('renders unavailable distinctly from an empty list', () => {
    const html = renderToStaticMarkup(
      <ContainerPills containers={[]} error="docker compose ps timed out after 10s" />,
    );
    expect(html).toContain('observability-containers-error');
    expect(html).toContain('Container list unavailable: docker compose ps timed out after 10s');
    expect(html).not.toContain('No containers reporting');
  });

  it('keeps the empty-but-healthy copy without an error', () => {
    const html = renderToStaticMarkup(<ContainerPills containers={[]} />);
    expect(html).toContain('No containers reporting.');
    expect(html).not.toContain('unavailable');
  });
});
