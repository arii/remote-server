import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import ContainerDrawer, { restartActionFor } from './ContainerDrawer';
import { dangerEntryFor } from './DangerZone';
import type { ContainerStatsRow } from '../apiClient';

function row(over: Partial<ContainerStatsRow> = {}): ContainerStatsRow {
  return {
    name: 'remote-terminal-1',
    service: 'terminal',
    state: 'running',
    cpu_pct: 2.5,
    mem_used: '1.2GiB',
    mem_limit: '7.8GiB',
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
      onRefresh={() => {}}
      onClose={() => {}}
      {...(over as object)}
    />,
  );
}

describe('container drawer (issue #663)', () => {
  it('maps restartable services to engine actions', () => {
    expect(restartActionFor('terminal')).toBe('recreate');
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

  it('offers confirm-guarded restart only on mapped rows', () => {
    const html = renderDrawer({
      rows: [
        row(),
        row({ name: 'remote-ollama-1', service: 'ollama' }),
      ],
    });
    expect(html).toContain('data-testid="container-restart-terminal"');
    expect(html).toContain('aria-label="Restart (asks for confirmation)"');
    expect(html).not.toContain('container-restart-ollama');
    expect(dangerEntryFor('recreate')).toEqual({ confirm: 'Live terminal sessions will drop.' });
  });

  it('shows the requested note after dispatch', () => {
    const html = renderDrawer({ restartRequested: 'recreate' });
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
