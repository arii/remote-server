import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  __clearPrCaches,
  abortAgentSession,
  abortRepair,
  apiUrl,
  chatStreamUrl,
  fetchAgentProviders,
  fetchAttention,
  fetchBriefing,
  fetchInbox,
  fetchJobs,
  fetchLedger,
  fetchPrDetails,
  fetchPrDiff,
  fetchSystemHealth,
  fetchVerifyCommand,
  fetchWorkflowHealth,
  invalidateViewCaches,
  postAgentTurn,
  startJobRun,
  warmViewCaches,
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
  invalidateViewCaches();
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

  it('fetches repair verify previews', async () => {
    const preview = { ok: true, issue: 9, command: 'pytest', allowlisted: true, error: '' };
    vi.stubGlobal('fetch', mockFetchJson(preview));
    expect(await fetchVerifyCommand('9')).toEqual(preview);
    expect(fetch).toHaveBeenCalledWith('/api/repair/verify-command?issue=9');
  });

  it('posts repair aborts', async () => {
    vi.stubGlobal('fetch', mockFetchJson({ ok: true, killed: true }));
    expect(await abortRepair()).toEqual({ ok: true, killed: true });
    expect(fetch).toHaveBeenCalledWith(
      '/api/repair/abort',
      expect.objectContaining({ method: 'POST' }),
    );
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

  it('fetches the aggregated inbox feed for a repo', async () => {
    const feed = {
      repo: 'remote-server',
      updated_at: 1,
      attention: [],
      merge_ready: { count: 0, top: [] },
      runs: { active: null, recent: [] },
    };
    vi.stubGlobal('fetch', mockFetchJson(feed));
    expect(await fetchInbox('remote-server')).toEqual(feed);
    expect(fetch).toHaveBeenCalledWith('/api/inbox?repo=remote-server');
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
    vi.stubGlobal('fetch', mockFetchJson({ providers: ['muse', 'agy'] }));
    expect(await fetchAgentProviders()).toEqual({ providers: ['muse', 'agy'] });
    expect(fetch).toHaveBeenCalledWith('/api/agent/providers');
  });

  it('starts a conversation behind hold-to-confirm', async () => {
    const resp = { ok: true, status: 200 };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(resp));
    expect(await postAgentTurn('muse', 'remote-server', 'hi', null)).toBe(resp);
    expect(fetch).toHaveBeenCalledWith('/api/agent/turn', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        provider: 'muse',
        repo: 'remote-server',
        prompt: 'hi',
        session_id: '',
        intent: 'agent-spawn',
        confirmed: true,
      }),
      signal: undefined,
    });
  });

  it('follows up on a session without reconfirming', async () => {
    const resp = { ok: true, status: 200 };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(resp));
    await postAgentTurn('agy', 'remote-server', 'again', 'abc123');
    expect(fetch).toHaveBeenCalledWith('/api/agent/turn', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        provider: 'agy',
        repo: 'remote-server',
        prompt: 'again',
        session_id: 'abc123',
        intent: '',
        confirmed: false,
      }),
      signal: undefined,
    });
  });

  it('aborts an agent turn', async () => {
    vi.stubGlobal('fetch', mockFetchJson({ ok: true, killed: true }));
    expect(await abortAgentSession('abc123')).toEqual({ ok: true, killed: true });
    expect(fetch).toHaveBeenCalledWith('/api/agent/abort', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: 'abc123' }),
    });
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

describe('view cache (briefing/jobs/attention)', () => {
  const jobsBody = { jobs: [] };
  const briefingBody = { attention: [] };
  const attentionBody = { updated_at: 1, items: [] };

  function queueJson(...bodies: unknown[]) {
    const f = vi.fn();
    for (const b of bodies) {
      f.mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => b,
        text: async () => '',
      });
    }
    return f;
  }

  it('serves briefing from cache within TTL and refetches after expiry', async () => {
    vi.useFakeTimers();
    try {
      const fetchMock = mockFetchJson(briefingBody);
      vi.stubGlobal('fetch', fetchMock);
      await fetchBriefing('demo');
      await fetchBriefing('demo');
      expect(fetchMock).toHaveBeenCalledTimes(1);
      vi.advanceTimersByTime(61_000);
      await fetchBriefing('demo');
      expect(fetchMock).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it('dedups concurrent jobs fetches into one request', async () => {
    const fetchMock = mockFetchJson(jobsBody);
    vi.stubGlobal('fetch', fetchMock);
    await Promise.all([fetchJobs(), fetchJobs()]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('never caches failures', async () => {
    vi.stubGlobal('fetch', mockFetchJson({ detail: 'boom' }, false, 500));
    await expect(fetchBriefing('demo')).rejects.toThrow('GET api/briefing');
    const succeeding = mockFetchJson(briefingBody);
    vi.stubGlobal('fetch', succeeding);
    await expect(fetchBriefing('demo')).resolves.toEqual(briefingBody);
    expect(succeeding).toHaveBeenCalledTimes(1);
  });

  it('mutations invalidate the jobs cache', async () => {
    const fetchMock = queueJson(jobsBody, { id: 'abc123' }, jobsBody);
    vi.stubGlobal('fetch', fetchMock);
    await fetchJobs();
    await startJobRun('merge-approved', 'demo', true);
    await fetchJobs();
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it('warms briefing/jobs/attention so later reads hit cache', async () => {
    const fetchMock = queueJson(briefingBody, jobsBody, attentionBody);
    vi.stubGlobal('fetch', fetchMock);
    await warmViewCaches('demo');
    await fetchBriefing('demo');
    await fetchJobs();
    await fetchAttention('demo');
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });
});

describe('chat stream url', () => {
  it('carries the backend prefer flag, defaulting to auto', () => {
    expect(chatStreamUrl('hi', 'r', 's')).toContain('prefer=auto');
    expect(chatStreamUrl('hi', 'r', 's', 'local')).toContain('prefer=local');
    expect(chatStreamUrl('hi', 'r', 's', 'cloud')).toContain('prefer=cloud');
  });
});
