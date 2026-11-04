import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import type { SystemHealthResponse, WorkflowHealthResponse, WorkflowRun } from '../apiClient';
import { ContainerPills, ObservabilityBody, RunRow } from './ObservabilitySection';

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

describe('independent stack/flow rendering (issue #474)', () => {
  const health = {
    disk: { summary: '12G free' },
    memory: { summary: '4G free' },
    containers: [
      { service: 'buttons', state: 'running' },
      { service: 'api', state: 'running' },
    ],
  } as SystemHealthResponse;

  it('flow error keeps healthy stack data visible alongside the runs error', () => {
    const html = renderToStaticMarkup(
      <ObservabilityBody health={health} flow={null} error="gh hiccup" />,
    );
    expect(html).toContain('observability-health');
    expect(html).toContain('2/2 containers up');
    expect(html).toContain('Disk: 12G free');
    expect(html).toContain('Mem: 4G free');
    expect(html).toContain('observability-containers');
    expect(html).toContain('buttons: running');
    expect(html).toContain('observability-error');
    expect(html).toContain('Automated runs unavailable: gh hiccup');
    expect(html).not.toContain('Observability unavailable');
  });

  it('flow error without stack health still shows the loading line, not a blank section', () => {
    const html = renderToStaticMarkup(
      <ObservabilityBody health={null} flow={null} error="gh hiccup" />,
    );
    expect(html).toContain('observability-health');
    expect(html).toContain('Stack health: loading');
    expect(html).toContain('observability-error');
  });

  it('healthy flow renders runs without any error', () => {
    const flow = {
      repo: 'o/r',
      status: 'failing',
      total_failing: 1,
      workflows: [
        {
          id: 1,
          name: 'CI',
          branch: 'main',
          event: 'push',
          diagnosed_cause: 'Tests failed.',
        },
      ],
    } as WorkflowHealthResponse;
    const html = renderToStaticMarkup(
      <ObservabilityBody health={health} flow={flow} error={null} />,
    );
    expect(html).toContain('observability-runs');
    expect(html).toContain('CI failed on main');
    expect(html).not.toContain('observability-error');
  });
});
