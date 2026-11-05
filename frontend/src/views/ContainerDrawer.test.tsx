import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import ContainerDrawer, {
  mergeStatsRows,
  restartActionFor,
  serviceRestartable,
  statsRowsFromHealth,
} from './ContainerDrawer';
import { dangerEntryFor } from './DangerZone';
import type { ContainerStatsRow, SystemHealthResponse } from '../apiClient';

function row(over: Partial<ContainerStatsRow> = {}): ContainerStatsRow {
  return {
    name: 'remote-terminal-1',
    service: 'terminal',
    state: 'running',
    status: 'Up 2 hours',
    health: '',
    cpu_pct: 2.5,
    mem_used: '1.2GiB',
    mem_limit: '7.8GiB',
    ...over,
  };
}

function health(over: Partial<SystemHealthResponse> = {}): SystemHealthResponse {
  return {
    disk: { summary: 'd' },
    memory: { summary: 'm' },
    containers: [],
    containers_error: null,
    ...over,
  };
}

function renderDrawer(over: Record<string, unknown> = {}): string {
  return renderToStaticMarkup(
    <ContainerDrawer
      rows={[row()]}
      loading={false}
      error={null}
      restartRequested={null}
      restartError={null}
      onRestart={() => {}}
      onRestartService={() => {}}
      onOpenLogs={() => {}}
      onRefresh={() => {}}
      onClose={() => {}}
      {...(over as object)}
    />,
  );
}

describe('container drawer (issue #663)', () => {
  it('maps restartable services to engine actions', () => {
    // Terminal has no restart path: sessions cannot survive a
    // recreate and there is no resume path (issue #444).
    expect(restartActionFor('terminal')).toBeNull();
    expect(restartActionFor('code-server')).toBe('recreate-code');
    expect(restartActionFor('buttons')).toBe('deploy-buttons');
    expect(restartActionFor('ollama')).toBeNull();
    expect(restartActionFor('caddy')).toBeNull();
  });

  it('renders one row per container with cpu/ram/state', () => {
    const html = renderDrawer({
      rows: [
        row(),
        row({ name: 'remote-ollama-1', service: 'ollama', cpu_pct: 0.1, mem_used: '3.1GiB' }),
      ],
    });
    expect(html).toContain('data-testid="container-drawer"');
    expect(html).toContain('remote-terminal-1');
    expect(html).toContain('remote-ollama-1');
    expect(html).toContain('2.5%');
    expect(html).toContain('1.2GiB / 7.8GiB');
    expect(html).toContain('running');
    expect(html).toContain('data-testid="container-refresh"');
    expect(html).toContain('data-testid="container-close"');
  });

  it('renders null usage as em dashes', () => {
    const html = renderDrawer({
      rows: [row({ state: 'exited', cpu_pct: null, mem_used: null, mem_limit: null })],
    });
    expect(html).toContain('exited');
    expect(html).not.toContain('null');
  });

  it('offers confirm-guarded engine restart only on mapped rows', () => {
    const html = renderDrawer({
      rows: [
        row({ name: 'remote-code-1', service: 'code-server' }),
        row({ name: 'remote-ollama-1', service: 'ollama' }),
      ],
    });
    expect(html).toContain('data-testid="container-restart-code-server"');
    expect(html).toContain('aria-label="Restart (asks for confirmation)"');
    expect(dangerEntryFor('recreate-code')).toEqual({ confirm: 'The code editor will restart.' });
  });

  it('offers no restart for terminal: sessions cannot survive it (issue #444)', () => {
    const html = renderDrawer();
    expect(html).toContain('remote-terminal-1');
    expect(html).not.toContain('container-restart-terminal');
  });

  it('shows the requested note after dispatch', () => {
    const html = renderDrawer({
      rows: [row({ name: 'remote-code-1', service: 'code-server' })],
      restartRequested: 'recreate-code',
    });
    expect(html).toContain('restart requested');
  });

  it('shows dispatch failures without dropping rows', () => {
    const html = renderDrawer({ restartError: 'a run is already in progress' });
    expect(html).toContain('data-testid="container-restart-error"');
    expect(html).toContain('already in progress');
    expect(html).toContain('remote-terminal-1');
  });

  it('shows loading and error states', () => {
    expect(renderDrawer({ loading: true, rows: [] })).toContain('Loading');
    expect(renderDrawer({ error: 'boom' })).toContain('boom');
  });
});

describe('container drawer logs + restart per row (issue #928)', () => {
  it('shows uptime from the compose status string', () => {
    const html = renderDrawer({
      rows: [row({ status: 'Up 51 minutes (healthy)' })],
    });
    expect(html).toContain('Up 51 minutes (healthy)');
  });

  it('shows an em dash when uptime is missing', () => {
    const html = renderDrawer({ rows: [row({ status: '' })] });
    expect(html).toContain('running · — · CPU');
  });

  it('offers a Logs button per container row', () => {
    const html = renderDrawer({
      rows: [
        row(),
        row({ name: 'remote-ollama-1', service: 'ollama' }),
      ],
    });
    expect(html).toContain('data-testid="container-logs-terminal"');
    expect(html).toContain('data-testid="container-logs-ollama"');
  });

  it('offers plain restart to unmapped non-terminal services', () => {
    expect(serviceRestartable('ollama')).toBe(true);
    expect(serviceRestartable('caddy')).toBe(true);
    expect(serviceRestartable('terminal')).toBe(false);
    expect(serviceRestartable('buttons')).toBe(false);
    expect(serviceRestartable('code-server')).toBe(false);
    expect(serviceRestartable('')).toBe(false);
    const html = renderDrawer({
      rows: [row({ name: 'remote-ollama-1', service: 'ollama' })],
    });
    expect(html).toContain('data-testid="container-restart-ollama"');
    expect(html).toContain('aria-label="Restart (asks for confirmation)"');
  });

  it('shows the service restart note after dispatch', () => {
    const html = renderDrawer({
      rows: [row({ name: 'remote-ollama-1', service: 'ollama' })],
      serviceRestarted: 'ollama',
    });
    expect(html).toContain('data-testid="container-service-restarted-ollama"');
    expect(html).toContain('the container is restarting');
  });

  it('shows a degraded note with rows when the live overlay fails', () => {
    const html = renderDrawer({ degraded: 'Live CPU/RAM unavailable — boom' });
    expect(html).toContain('data-testid="container-degraded"');
    expect(html).toContain('Live CPU/RAM unavailable — boom');
    expect(html).toContain('remote-terminal-1');
    expect(html).not.toContain('container-error');
  });

  it('derives rows from the health snapshot', () => {
    const rows = statsRowsFromHealth(
      health({
        containers: [
          { name: 'remote-caddy-1', service: 'caddy', state: 'running', status: 'Up 3m', health: '' },
        ],
      }),
    );
    expect(rows).toEqual([
      {
        name: 'remote-caddy-1',
        service: 'caddy',
        state: 'running',
        status: 'Up 3m',
        health: '',
        cpu_pct: null,
        mem_used: null,
        mem_limit: null,
      },
    ]);
  });

  it('derives no rows without health or with a list error', () => {
    expect(statsRowsFromHealth(null)).toEqual([]);
    expect(statsRowsFromHealth(health({ containers_error: 'boom' }))).toEqual([]);
  });

  it('merges live stats over health rows by service', () => {
    const base = statsRowsFromHealth(
      health({
        containers: [
          { name: 'remote-caddy-1', service: 'caddy', state: 'running', status: 'Up 3m', health: '' },
          { name: 'remote-x-1', service: 'x', state: 'exited', status: 'Exited', health: '' },
        ],
      }),
    );
    const merged = mergeStatsRows(base, [
      {
        name: 'remote-caddy-1', service: 'caddy', state: 'running', status: 'Up 4m',
        health: '', cpu_pct: 1.5, mem_used: '9MiB', mem_limit: '1GiB',
      },
      {
        name: 'remote-new-1', service: 'new', state: 'running',
        cpu_pct: 0.1, mem_used: '1MiB', mem_limit: '1GiB',
      },
    ]);
    expect(merged).toHaveLength(3);
    // Live CPU/RAM + fresher status win on the match.
    expect(merged[0]).toMatchObject({ service: 'caddy', status: 'Up 4m', cpu_pct: 1.5 });
    // No live match: health state/uptime survive.
    expect(merged[1]).toMatchObject({ service: 'x', status: 'Exited', cpu_pct: null });
    // Live-only rows append.
    expect(merged[2]).toMatchObject({ service: 'new', cpu_pct: 0.1 });
  });

  it('merges without mutating either input', () => {
    const base = [row({ cpu_pct: null })];
    const live = [row({ cpu_pct: 9.9 })];
    mergeStatsRows(base, live);
    expect(base[0].cpu_pct).toBeNull();
    expect(live[0].cpu_pct).toBe(9.9);
  });

});
