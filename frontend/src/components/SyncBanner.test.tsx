import { beforeEach, describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import App from '../App';
import { __clearOutbox, flushOutbox, getOutbox, startJobRun } from '../apiClient';
import SyncBanner from './SyncBanner';

function mockFetchJson(body: unknown, ok = true, status = 200) {
  return vi.fn().mockResolvedValue({
    ok,
    status,
    json: async () => body,
    text: async () => (ok ? '' : JSON.stringify(body)),
  });
}

beforeEach(() => {
  vi.unstubAllGlobals();
  __clearOutbox();
});

// One offline tap: the mutation queues instead of sending.
async function tapOffline(): Promise<void> {
  vi.stubGlobal('fetch', vi.fn());
  vi.stubGlobal('navigator', { onLine: false });
  await expect(startJobRun('logs', 'remote-server', false)).rejects.toThrow('queued offline');
  expect(getOutbox()).toHaveLength(1);
}

describe('SyncBanner (issue #404)', () => {
  it('renders nothing when online with an empty queue', () => {
    expect(renderToStaticMarkup(<SyncBanner />)).toBe('');
  });

  it('names the offline state before anything queues', () => {
    vi.stubGlobal('navigator', { onLine: false });
    const html = renderToStaticMarkup(<SyncBanner />);
    expect(html).toContain('data-testid="sync-banner"');
    expect(html).toContain('offline');
  });

  it('shows the queued count after an offline tap', async () => {
    await tapOffline();
    const html = renderToStaticMarkup(<SyncBanner />);
    expect(html).toContain('1 action queued');
  });

  it('offers sync and discard when back online with pendings', async () => {
    await tapOffline();
    vi.unstubAllGlobals();
    const html = renderToStaticMarkup(<SyncBanner />);
    expect(html).toContain('1 action queued');
    expect(html).toContain('data-testid="sync-retry"');
    expect(html).toContain('data-testid="sync-discard"');
  });

  it('clears once the queue syncs', async () => {
    await tapOffline();
    vi.unstubAllGlobals();
    vi.stubGlobal('fetch', mockFetchJson({ id: 'abc123def456' }));
    await flushOutbox();
    expect(renderToStaticMarkup(<SyncBanner />)).toBe('');
  });

  it('mounts in App so every view inherits it', async () => {
    await tapOffline();
    const html = renderToStaticMarkup(<App />);
    expect(html).toContain('data-testid="sync-banner"');
  });
});
