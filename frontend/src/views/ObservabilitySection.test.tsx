import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import type { SystemHealthResponse, WorkflowHealthResponse, WorkflowRun } from '../apiClient';
import { ContainerPills, HostTelemetry, RunRow, WorkflowRunsBody, runAge, telemetryText } from './ObservabilitySection';

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

  it('paints the failing edge without expanding (issue #964)', () => {
    const html = renderToStaticMarkup(<RunRow run={run} />);
    expect(html).toContain('class="runs-row tone-bad"');
  });
});

describe('container list failure (issue #530)', () => {
  it('renders unavailable distinctly from an empty list', () => {
    const html = renderToStaticMarkup(
      <ContainerPills containers={[]} error="docker compose ps timed out after 10s" />,
    );
    expect(html).toContain('container-pills-error');
    expect(html).toContain('Container list unavailable: docker compose ps timed out after 10s');
    expect(html).not.toContain('No containers reporting');
  });

  it('keeps the empty-but-healthy copy without an error', () => {
    const html = renderToStaticMarkup(<ContainerPills containers={[]} />);
    expect(html).toContain('No containers reporting.');
    expect(html).not.toContain('unavailable');
  });

  it('renders pills as buttons that open container logs (issues #896, #928)', () => {
    const html = renderToStaticMarkup(
      <ContainerPills
        containers={[{ service: 'buttons', state: 'running', health: 'healthy' }]}
        onOpenStats={() => {}}
      />,
    );
    expect(html).toContain('data-testid="container-pill"');
    expect(html).toContain('buttons: running (healthy) — open container logs');
    expect(html).toContain('<button');
  });
});

describe('container history filter (issue #931)', () => {
  it('offers a History action per pill that filters the feed', () => {
    const html = renderToStaticMarkup(
      <ContainerPills
        containers={[
          { service: 'buttons', state: 'running', health: 'healthy' },
          { service: 'caddy', state: 'running' },
        ]}
        onOpenStats={() => {}}
        onFilterHistory={() => {}}
      />,
    );
    expect(html).toContain('data-testid="container-history-buttons"');
    expect(html).toContain('data-testid="container-history-caddy"');
    expect(html).toContain('buttons: running (healthy) — filter history');
    expect(html).toContain('>History</button>');
    // The logs pill keeps its own entry point.
    expect(html).toContain('— open container logs');
  });

  it('renders no History action without a filter handler', () => {
    const html = renderToStaticMarkup(
      <ContainerPills
        containers={[{ service: 'buttons', state: 'running', health: 'healthy' }]}
        onOpenStats={() => {}}
      />,
    );
    expect(html).not.toContain('container-history-');
  });
});

describe('host telemetry line (issue #931)', () => {
  it('joins Disk and Mem summaries, omitting missing parts', () => {
    expect(telemetryText('12G free', '4G free')).toBe('Disk: 12G free · Mem: 4G free');
    expect(telemetryText('12G free', null)).toBe('Disk: 12G free');
    expect(telemetryText(null, '4G free')).toBe('Mem: 4G free');
    expect(telemetryText(null, null)).toBeNull();
    expect(telemetryText('', '')).toBeNull();
  });

  function health(): SystemHealthResponse {
    return {
      disk: { summary: '12G free' },
      memory: { summary: '4G free' },
      containers: [],
    } as SystemHealthResponse;
  }

  it('renders the Disk/Mem line from the health snapshot', () => {
    const html = renderToStaticMarkup(<HostTelemetry health={health()} />);
    expect(html).toContain('data-testid="host-telemetry"');
    expect(html).toContain('Disk: 12G free');
    expect(html).toContain('Mem: 4G free');
  });

  it('loads while the snapshot is missing, hides when summaries are', () => {
    const loading = renderToStaticMarkup(<HostTelemetry health={null} />);
    expect(loading).toContain('data-testid="host-telemetry"');
    expect(loading).toContain('Host telemetry: loading');
    const bare = renderToStaticMarkup(
      <HostTelemetry health={{ disk: {}, memory: {}, containers: [] }} />,
    );
    expect(bare).not.toContain('host-telemetry');
  });
});

describe('workflow runs section (issue #931)', () => {
  function flow(): WorkflowHealthResponse {
    return {
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
  }

  it('renders failing runs without live infra state', () => {
    const html = renderToStaticMarkup(<WorkflowRunsBody flow={flow()} error={null} />);
    expect(html).toContain('workflow-runs');
    expect(html).toContain('CI failed on main');
    expect(html).not.toContain('workflow-runs-error');
    expect(html).not.toContain('containers up');
    expect(html).not.toContain('Disk:');
  });

  it('reports the runs error without a stack fallback', () => {
    const html = renderToStaticMarkup(<WorkflowRunsBody flow={null} error="gh hiccup" />);
    expect(html).toContain('workflow-runs-error');
    expect(html).toContain('Automated runs unavailable: gh hiccup');
  });

  it('loads while the flow snapshot is missing', () => {
    const html = renderToStaticMarkup(<WorkflowRunsBody flow={null} error={null} />);
    expect(html).toContain('workflow-runs-loading');
    expect(html).toContain('Loading automated runs…');
  });

  it('reassures when no automated runs are failing', () => {
    const html = renderToStaticMarkup(
      <WorkflowRunsBody flow={{ ...flow(), workflows: [] }} error={null} />,
    );
    expect(html).toContain('workflow-runs-empty');
    expect(html).toContain('No failing automated runs');
  });

  it('threads feed search to URL-less runs, hides it without a callback', () => {
    // Rebase regression lock (#947 review): WorkflowRunsBody must
    // forward onFindInFeed so URL-less rows keep one-click search.
    const withFinder = renderToStaticMarkup(
      <WorkflowRunsBody flow={flow()} error={null} onFindInFeed={() => {}} />,
    );
    expect(withFinder).toContain('data-testid="observability-find-1"');
    expect(withFinder).toContain('Find in feed');
    const withoutFinder = renderToStaticMarkup(
      <WorkflowRunsBody flow={flow()} error={null} />,
    );
    expect(withoutFinder).not.toContain('observability-find-1');
  });
});

describe('observability actionable rows (issue #907)', () => {
  it('finds URL-less runs in the feed', () => {
    const html = renderToStaticMarkup(
      <RunRow
        run={{ id: 9, name: 'Nightly', diagnosed_cause: 'Flaky.' } as WorkflowRun}
        onFindInFeed={() => {}}
      />,
    );
    expect(html).toContain('data-testid="observability-find-9"');
    expect(html).toContain('Find in feed');
  });

  it('reports the runs error without a retry control', () => {
    // Issue #931 moved live telemetry out of History: the runs
    // section surfaces a plain error/loading state (covered above
    // against WorkflowRunsBody), not the old retry control.
    const html = renderToStaticMarkup(
      <WorkflowRunsBody flow={null} error="gh hiccup" />,
    );
    expect(html).toContain('data-testid="workflow-runs-error"');
    expect(html).not.toContain('observability-retry');
  });
});

describe('run age (issue #1180)', () => {
  const NOW = Date.parse('2026-11-01T12:00:00Z');

  beforeEach(() => {
    vi.spyOn(Date, 'now').mockReturnValue(NOW);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  function run(overrides: Partial<WorkflowRun> = {}): WorkflowRun {
    return {
      id: 41760116751,
      name: 'CI',
      branch: 'fix/x',
      event: 'push',
      conclusion: 'failure',
      url: 'https://github.com/o/r/actions/runs/41760116751',
      diagnosed_cause: 'Tests failed.',
      ...overrides,
    } as WorkflowRun;
  }

  const iso = (ms: number): string => new Date(ms).toISOString();

  it('runAge names just-now, minutes, hours, and days', () => {
    expect(runAge(iso(NOW - 10_000))).toBe('just now');
    expect(runAge(iso(NOW - 5 * 60_000))).toBe('5m ago');
    expect(runAge(iso(NOW - 3 * 3_600_000))).toBe('3h ago');
    expect(runAge(iso(NOW - 2 * 86_400_000))).toBe('2d ago');
  });

  it('runAge admits unknown time for a missing or garbage stamp', () => {
    expect(runAge(undefined)).toBe('unknown time');
    expect(runAge('not-a-date')).toBe('unknown time');
  });

  it('RunRow shows the run id with its age from created_at', () => {
    const html = renderToStaticMarkup(
      <RunRow run={run({ created_at: iso(NOW - 5 * 60_000) })} />,
    );
    expect(html).toContain('data-testid="observability-run-age-41760116751"');
    expect(html).toContain('#41760116751 · 5m ago');
  });

  it('RunRow prefers created_at over updated_at', () => {
    const html = renderToStaticMarkup(
      <RunRow
        run={run({
          created_at: iso(NOW - 5 * 60_000),
          updated_at: iso(NOW - 3 * 3_600_000),
        })}
      />,
    );
    expect(html).toContain('5m ago');
    expect(html).not.toContain('3h ago');
  });

  it('RunRow falls back to updated_at without created_at', () => {
    const html = renderToStaticMarkup(
      <RunRow run={run({ updated_at: iso(NOW - 2 * 60_000) })} />,
    );
    expect(html).toContain('#41760116751 · 2m ago');
  });

  it('RunRow admits unknown time without any stamp', () => {
    const html = renderToStaticMarkup(<RunRow run={run()} />);
    expect(html).toContain('data-testid="observability-run-age-41760116751"');
    expect(html).toContain('unknown time');
  });
});
