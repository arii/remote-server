// @vitest-environment happy-dom
// StatusStrip props contract (D5): presentational only — facts ride
// props from the App-level single sources, pills hide when not
// loaded, and a tap opens the triage drawer. Rendered standalone so
// the component doubles as its own preview.
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { describe, expect, it, vi } from 'vitest';
import StatusStrip, { stripHealthState } from './StatusStrip';
import type { SystemHealthResponse } from '../apiClient';

function healthFixture(
  containers: Array<{ state?: string; health?: string }>,
  containers_error?: string | null,
): SystemHealthResponse {
  return { disk: {}, memory: {}, containers, containers_error };
}

function renderStrip(props: Parameters<typeof StatusStrip>[0]) {
  const host = document.createElement('div');
  document.body.appendChild(host);
  let root: Root | null = null;
  const teardown = () => {
    act(() => root?.unmount());
    host.remove();
  };
  act(() => {
    root = createRoot(host);
    root.render(<StatusStrip {...props} />);
  });
  return { host, teardown };
}

describe('stripHealthState derivation', () => {
  it('reads loading before the first snapshot arrives', () => {
    expect(stripHealthState(null)).toBe('loading');
  });

  it('reads ok only when every container is running and healthy', () => {
    expect(stripHealthState(healthFixture([{ state: 'running' }, { state: 'running', health: 'healthy' }]))).toBe('ok');
  });

  it('reads degraded when a container is down or unhealthy', () => {
    expect(stripHealthState(healthFixture([{ state: 'running' }, { state: 'exited' }]))).toBe('degraded');
    expect(stripHealthState(healthFixture([{ state: 'running', health: 'unhealthy' }]))).toBe('degraded');
    expect(stripHealthState(healthFixture([]))).toBe('degraded');
  });

  it('reads error when the container list is unavailable', () => {
    expect(stripHealthState(healthFixture([], 'docker unreachable'))).toBe('error');
  });
});

describe('StatusStrip props contract', () => {
  it('renders the health pill with its state and label', () => {
    const { host, teardown } = renderStrip({
      healthState: 'ok',
      healthLabel: 'Status: 5/5 containers up',
      attentionCount: null,
      openPrCount: null,
      onTap: () => {},
    });
    const health = host.querySelector('[data-testid="status-health"]');
    expect(health?.textContent).toBe('Status: 5/5 containers up');
    expect(host.querySelector('[data-testid="status-strip"]')?.getAttribute('data-health-state')).toBe('ok');
    teardown();
  });

  it('hides unloaded counts instead of flashing a wrong zero', () => {
    const { host, teardown } = renderStrip({
      healthState: 'loading',
      healthLabel: 'Status: loading…',
      attentionCount: null,
      openPrCount: null,
      onTap: () => {},
    });
    expect(host.querySelector('[data-testid="status-attention"]')).toBeNull();
    expect(host.querySelector('[data-testid="status-prs"]')).toBeNull();
    teardown();
  });

  it('shows loaded attention and PR counts, singular and plural', () => {
    const { host, teardown } = renderStrip({
      healthState: 'ok',
      healthLabel: 'Status: 5/5 containers up',
      attentionCount: 1,
      openPrCount: 3,
      onTap: () => {},
    });
    expect(host.querySelector('[data-testid="status-attention"]')?.textContent).toBe('1 needs attention');
    expect(host.querySelector('[data-testid="status-prs"]')?.textContent).toBe('3 open PRs');
    teardown();
  });

  it('taps open the triage drawer', () => {
    const onTap = vi.fn();
    const { host, teardown } = renderStrip({
      healthState: 'degraded',
      healthLabel: 'Status: 4/5 containers up',
      attentionCount: 2,
      openPrCount: 0,
      onTap,
    });
    const strip = host.querySelector('[data-testid="status-strip"]');
    expect(strip?.getAttribute('data-health-state')).toBe('degraded');
    act(() => {
      (strip as HTMLButtonElement).click();
    });
    expect(onTap).toHaveBeenCalledTimes(1);
    teardown();
  });
});
