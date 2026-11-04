import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  __clearPrCaches,
  abortAgentSession,
  abortRepair,
  apiUrl,
  approvePr,
  chatStreamUrl,
  fetchAgentProviders,
  fetchAttention,
  fetchBriefing,
  fetchInbox,
  fetchJobs,
  fetchLedger,
  fetchNotificationStatus,
  fetchPrDetails,
  fetchPrDiff,
  fetchRouteHint,
  fetchSystemHealth,
  fetchVerifyCommand,
  fetchWorkflowHealth,
  invalidateViewCaches,
  postAgentTurn,
  requestPrChanges,
  snoozeAttention,
  startJobRun,
  subscribeJobsPush,
  subscribeRunPush,
  triggerDigest,
  unsnoozeAttention,
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

  it('fetches notification status (issue #615)', async () => {
    const status = { ntfy_configured: true, last_digest: 5 };
    vi.stubGlobal('fetch', mockFetchJson(status));
    expect(await fetchNotificationStatus()).toEqual(status);
    expect(fetch).toHaveBeenCalledWith('/api/notifications/status');
  });

  it('posts digest triggers with an empty body (issue #615)', async () => {
    const res = { ok: true, results: {}, digest: 'd' };
    vi.stubGlobal('fetch', mockFetchJson(res));
    expect(await triggerDigest()).toEqual(res);
    expect(fetch).toHaveBeenCalledWith(
      '/api/notifications/trigger-digest',
      expect.objectContaining({ method: 'POST', body: '{}' }),
    );
  });

  it('posts attention snoozes with id, minutes, repo (issue #614)', async () => {
    const res = { ok: true, id: 'job-j1', snoozed_until: 9 };
    vi.stubGlobal('fetch', mockFetchJson(res));
    expect(await snoozeAttention('job-j1', 60, 'demo')).toEqual(res);
    expect(fetch).toHaveBeenCalledWith(
      '/api/attention/snooze',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ id: 'job-j1', minutes: 60, repo: 'demo' }),
      }),
    );
  });

  it('posts attention unsnoozes with id (issue #614)', async () => {
    const res = { ok: true, id: 'job-j1', snoozed: false };
    vi.stubGlobal('fetch', mockFetchJson(res));
    expect(await unsnoozeAttention('job-j1')).toEqual(res);
    expect(fetch).toHaveBeenCalledWith(
      '/api/attention/unsnooze',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ id: 'job-j1' }),
      }),
    );
  });

  it('fetches ledger entries', async () => {
    const ledger = { entries: [] };
    vi.stubGlobal('fetch', mockFetchJson(ledger));
    expect(await fetchLedger('all', 10)).toEqual(ledger);
    expect(fetch).toHaveBeenCalledWith('/api/ledger?kind=all&limit=10');
  });

  it('passes since for windowed ledger reads (issue #609)', async () => {
    const ledger = { entries: [] };
    vi.stubGlobal('fetch', mockFetchJson(ledger));
    expect(await fetchLedger('all', 200, 123.5)).toEqual(ledger);
    expect(fetch).toHaveBeenCalledWith('/api/ledger?kind=all&limit=200&since=123.5');
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

describe('route hint', () => {
  it('fetches the hint for the picked backend', async () => {
    const body = {
      ok: true,
      provider: 'nebius',
      model: 'm',
      metered: true,
      rate_in_per_1m: 0.15,
      rate_out_per_1m: 0.5,
      error: '',
    };
    const fetchMock = mockFetchJson(body);
    vi.stubGlobal('fetch', fetchMock);
    const hint = await fetchRouteHint('cloud');
    expect(hint).toEqual(body);
    expect(String(fetchMock.mock.calls[0][0])).toContain('prefer=cloud');
  });
});

describe('jobs push subscriptions', () => {
  interface FakeSource {
    url: string;
    listeners: Record<string, Array<(e: { data: string }) => void>>;
    onmessage: ((e: { data: string }) => void) | null;
    onerror: (() => void) | null;
    closed: boolean;
    addEventListener(type: string, fn: (e: { data: string }) => void): void;
    close(): void;
    emit(type: string, data: string): void;
    fail(): void;
  }

  function makeFactory(seen: FakeSource[]) {
    return (url: string): FakeSource => {
      const src: FakeSource = {
        url,
        listeners: {},
        onmessage: null,
        onerror: null,
        closed: false,
        addEventListener(type, fn) {
          (src.listeners[type] ??= []).push(fn);
        },
        close() {
          src.closed = true;
        },
        emit(type, data) {
          for (const fn of src.listeners[type] ?? []) fn({ data });
        },
        fail() {
          src.onerror?.();
        },
      };
      seen.push(src);
      return src;
    };
  }

  it('streams job lists and reports liveness', () => {
    const seen: FakeSource[] = [];
    const jobs: unknown[] = [];
    const live: boolean[] = [];
    const unsub = subscribeJobsPush(
      {
        onJobs: (j) => jobs.push(j),
        onLive: (l) => live.push(l),
      },
      makeFactory(seen),
    );
    expect(seen[0].url).toContain('api/jobs/stream');
    seen[0].emit('jobs', JSON.stringify({ jobs: [{ id: 'a' }] }));
    expect(live).toEqual([true]);
    expect(jobs).toEqual([[{ id: 'a' }]]);
    seen[0].emit('jobs', 'not json');
    expect(jobs).toHaveLength(1);
    unsub();
    expect(seen[0].closed).toBe(true);
  });

  it('closes on channel error and reports dead', () => {
    const seen: FakeSource[] = [];
    const live: boolean[] = [];
    subscribeJobsPush({ onJobs: () => {}, onLive: (l) => live.push(l) }, makeFactory(seen));
    seen[0].fail();
    expect(live).toEqual([false]);
    expect(seen[0].closed).toBe(true);
  });

  it('streams run progress, ends on done, falls back on death', () => {
    const seen: FakeSource[] = [];
    const tails: string[] = [];
    let done = 0;
    let dead = 0;
    subscribeRunPush(
      'abc123def456',
      {
        onProgress: (p) => tails.push(p.output),
        onDone: () => done++,
        onDead: () => dead++,
      },
      makeFactory(seen),
    );
    expect(seen[0].url).toContain('api/jobs/runs/abc123def456/stream');
    seen[0].emit(
      'progress',
      JSON.stringify({ done: false, output: 'half', exit_code: null, action: 'logs' }),
    );
    expect(tails).toEqual(['half']);
    seen[0].emit(
      'progress',
      JSON.stringify({ done: true, output: 'full', exit_code: 0, action: 'logs' }),
    );
    expect(done).toBe(1);
    expect(seen[0].closed).toBe(true);
    // Death before done triggers fallback; after done it is ignored.
    const seen2: FakeSource[] = [];
    let dead2 = 0;
    subscribeRunPush(
      'abc123def456',
      { onProgress: () => {}, onDone: () => {}, onDead: () => dead2++ },
      makeFactory(seen2),
    );
    seen2[0].fail();
    expect(dead2).toBe(1);
    expect(dead).toBe(0);
  });

  it('treats a gone frame as dead', () => {
    const seen: FakeSource[] = [];
    let dead = 0;
    subscribeRunPush(
      'abc123def456',
      { onProgress: () => {}, onDone: () => {}, onDead: () => dead++ },
      makeFactory(seen),
    );
    seen[0].emit('gone', JSON.stringify({ error: 'unknown run' }));
    expect(dead).toBe(1);
    expect(seen[0].closed).toBe(true);
  });
});

describe('pr review actions (issue #616)', () => {
  it('approves with the pr-approve confirm intent', async () => {
    vi.stubGlobal('fetch', mockFetchJson({ ok: true, output: 'approved' }));
    expect(await approvePr('remote-server', 7)).toEqual({ ok: true, output: 'approved' });
    expect(fetch).toHaveBeenCalledWith('/api/pr-action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        repo: 'remote-server',
        pr_number: 7,
        action: 'approve',
        intent: 'pr-approve',
        confirmed: true,
      }),
    });
  });

  it('requests changes with the body and pr-request-changes intent', async () => {
    vi.stubGlobal('fetch', mockFetchJson({ ok: true, output: 'reviewed' }));
    expect(await requestPrChanges('remote-server', 7, 'Fix the leak.')).toEqual({
      ok: true,
      output: 'reviewed',
    });
    expect(fetch).toHaveBeenCalledWith('/api/pr-action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        repo: 'remote-server',
        pr_number: 7,
        action: 'request-changes',
        body: 'Fix the leak.',
        intent: 'pr-request-changes',
        confirmed: true,
      }),
    });
  });

  it('surfaces endpoint errors to the row', async () => {
    vi.stubGlobal('fetch', mockFetchJson({ detail: 'nope' }, false, 400));
    await expect(approvePr('remote-server', 7)).rejects.toThrow('POST api/pr-action failed: 400');
  });
});
