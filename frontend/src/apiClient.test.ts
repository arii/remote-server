import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  __clearPrCaches,
  abortAgentSession,
  agentWsUrl,
  apiUrl,
  fetchAgentProviders,
  fetchAttention,
  fetchJobs,
  fetchLedger,
  fetchPrDetails,
  fetchPrDiff,
  fetchSystemHealth,
  fetchWorkflowHealth,
  spawnAgentSession,
} from './apiClient';

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
  vi.useRealTimers();
  __clearPrCaches();
});

describe('apiClient (fetch with inline shapes)', () => {
  it('fetches the attention queue', async () => {
    const queue = { updated_at: 1, items: [] };
    vi.stubGlobal('fetch', mockFetchJson(queue));
    expect(await fetchAttention('remote-server')).toEqual(queue);
    expect(fetch).toHaveBeenCalledWith('/api/attention?repo=remote-server');
  });

  it('fetches jobs', async () => {
    const jobs = { jobs: [] };
    vi.stubGlobal('fetch', mockFetchJson(jobs));
    expect(await fetchJobs()).toEqual(jobs);
    expect(fetch).toHaveBeenCalledWith('/api/jobs');
  });

  it('fetches ledger entries', async () => {
    const ledger = { entries: [] };
    vi.stubGlobal('fetch', mockFetchJson(ledger));
    expect(await fetchLedger('all', 10)).toEqual(ledger);
    expect(fetch).toHaveBeenCalledWith('/api/ledger?kind=all&limit=10');
  });

  it('fetches system health', async () => {
    const sys = { disk: {}, memory: {}, containers: [] };
    vi.stubGlobal('fetch', mockFetchJson(sys));
    expect(await fetchSystemHealth()).toEqual(sys);
    expect(fetch).toHaveBeenCalledWith('/api/system/health');
  });

  it('fetches workflow health', async () => {
    const health = { repo: 'demo', status: 'ok', total_failing: 0, workflows: [] };
    vi.stubGlobal('fetch', mockFetchJson(health));
    expect(await fetchWorkflowHealth('demo')).toEqual(health);
    expect(fetch).toHaveBeenCalledWith('/api/workflow-health?repo=demo');
  });

  it('throws visibly on HTTP errors (no silent fallback)', async () => {
    vi.stubGlobal('fetch', mockFetchJson({ detail: 'boom' }, false, 500));
    await expect(fetchJobs()).rejects.toThrow('GET api/jobs failed: 500');
  });

  it('fetches the pr diff for a repo and number', async () => {
    vi.stubGlobal('fetch', mockFetchJson({ number: 1095, diff: 'x', truncated: false }));
    await fetchPrDiff('boomtick', 1095);
    expect(fetch).toHaveBeenCalledWith('/api/pr-diff?repo=boomtick&pr=1095');
  });

  it('absolutizes api paths from the Vite base, passes the rest through', () => {
    expect(apiUrl('api/jobs')).toBe('/api/jobs');
    expect(apiUrl('/api/jobs')).toBe('/api/jobs');
    expect(apiUrl('https://example.com/x')).toBe('https://example.com/x');
  });

  it('fetches the agent provider allowlist', async () => {
    vi.stubGlobal('fetch', mockFetchJson({ providers: ['muse', 'agy', 'pi'] }));
    expect(await fetchAgentProviders()).toEqual({ providers: ['muse', 'agy', 'pi'] });
    expect(fetch).toHaveBeenCalledWith('/api/agent/providers');
  });

  it('spawns an agent session behind hold-to-confirm', async () => {
    vi.stubGlobal('fetch', mockFetchJson({ session_id: 'abc123' }));
    expect(await spawnAgentSession('muse', 'remote-server')).toEqual({ session_id: 'abc123' });
    expect(fetch).toHaveBeenCalledWith('/api/agent/spawn', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        provider: 'muse',
        repo: 'remote-server',
        intent: 'agent-spawn',
        confirmed: true,
      }),
    });
  });

  it('aborts an agent session', async () => {
    vi.stubGlobal('fetch', mockFetchJson({ ok: true, exit: 143 }));
    expect(await abortAgentSession('abc123')).toEqual({ ok: true, exit: 143 });
    expect(fetch).toHaveBeenCalledWith('/api/agent/abort', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: 'abc123' }),
    });
  });

  it('builds ws(s) agent relay urls from the page origin', () => {
    expect(agentWsUrl('https:', 'example.com', 'abc123')).toBe(
      'wss://example.com/api/agent/ws/abc123',
    );
    expect(agentWsUrl('http:', 'localhost:5173', 'abc123')).toBe(
      'ws://localhost:5173/api/agent/ws/abc123',
    );
  });
});

describe('pr details/diff cache + in-flight dedup', () => {
  const detailsBody = { number: 7, title: 'cached' };
  const diffBody = { number: 7, diff: 'x', truncated: false };

  it('dedups concurrent details fetches into one network request', async () => {
    const fetchMock = mockFetchJson(detailsBody);
    vi.stubGlobal('fetch', fetchMock);
    const [a, b] = await Promise.all([
      fetchPrDetails('demo', 7),
      fetchPrDetails('demo', 7),
    ]);
    expect(a).toEqual(detailsBody);
    expect(b).toEqual(detailsBody);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('serves cached details within TTL and refetches after expiry', async () => {
    vi.useFakeTimers();
    try {
      const fetchMock = mockFetchJson(detailsBody);
      vi.stubGlobal('fetch', fetchMock);
      await fetchPrDetails('demo', 8);
      await fetchPrDetails('demo', 8);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      vi.advanceTimersByTime(61_000);
      await fetchPrDetails('demo', 8);
      expect(fetchMock).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it('never caches failures', async () => {
    const failing = mockFetchJson({ detail: 'boom' }, false, 500);
    vi.stubGlobal('fetch', failing);
    await expect(fetchPrDetails('demo', 9)).rejects.toThrow('GET api/pr-details');
    const succeeding = mockFetchJson(detailsBody);
    vi.stubGlobal('fetch', succeeding);
    await expect(fetchPrDetails('demo', 9)).resolves.toEqual(detailsBody);
    expect(failing).toHaveBeenCalledTimes(1);
    expect(succeeding).toHaveBeenCalledTimes(1);
    // The retry success is cached.
    await fetchPrDetails('demo', 9);
    expect(succeeding).toHaveBeenCalledTimes(1);
  });

  it('dedups concurrent diff fetches and serves diffs from cache', async () => {
    const fetchMock = mockFetchJson(diffBody);
    vi.stubGlobal('fetch', fetchMock);
    const [a, b] = await Promise.all([
      fetchPrDiff('demo', 7),
      fetchPrDiff('demo', 7),
    ]);
    expect(a).toEqual(diffBody);
    expect(b).toEqual(diffBody);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await fetchPrDiff('demo', 7);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
