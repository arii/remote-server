import { beforeEach, describe, expect, it, vi } from 'vitest';
import { plainLanguage } from './views/historyUtils';
import {
  API_SLOW_TIMEOUT_MS,
  API_TIMEOUT_MS,
  AuthExpiredError,
  QueuedOfflineError,
  RequestTimeoutError,
  __clearAuthExpired,
  __clearNetworkFailures,
  __clearOutbox,
  __clearPrCaches,
  abortAgentSession,
  abortRepair,
  apiUrl,
  cancelJobRun,
  approvePr,
  clearOutbox,
  createIssue,
  dispatchRepair,
  fetchAgentProviders,
  fetchAttention,
  fetchBriefing,
  fetchContainerLogs,
  fetchContainerStats,
  fetchInbox,
  fetchJobs,
  fetchDigestSchedule,
  fetchLedger,
  fetchNotificationStatus,
  fetchNotificationPreferences,
  fetchPrDetails,
  fetchPrDiff,
  fetchRepoDiff,
  fetchRepoFiles,
  fetchRouteHint,
  fetchRunCompare,
  fetchSystemHealth,
  fetchVerifyCommand,
  fetchWorkflowHealth,
  flushOutbox,
  getOutbox,
  getOutboxStatus,
  invalidateViewCaches,
  isAuthExpired,
  isAuthRedirect,
  mergePr,
  postAgentTurn,
  postChatStream,
  refreshBriefing,
  repoAction,
  requestPrChanges,
  rerunJob,
  restartContainer,
  restoreRow,
  setDigestSchedule,
  saveNotificationPreferences,
  snoozeAttention,
  startJobRun,
  startTdRun,
  subscribeApiFailures,
  throwIfAuthExpired,
  subscribeAuthExpired,
  subscribeJobsPush,
  subscribeOutbox,
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
  __clearAuthExpired();
  __clearNetworkFailures();
  __clearPrCaches();
  __clearOutbox();
  invalidateViewCaches();
});

describe('apiClient (fetch with inline shapes)', () => {
  it('fetches the attention queue', async () => {
    const queue = { updated_at: 1, items: [] };
    vi.stubGlobal('fetch', mockFetchJson(queue));
    expect(await fetchAttention('remote-server')).toEqual(queue);
    expect(fetch).toHaveBeenCalledWith(
      '/api/attention?repo=remote-server',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it('fetches jobs', async () => {
    const jobs = { jobs: [] };
    vi.stubGlobal('fetch', mockFetchJson(jobs));
    expect(await fetchJobs()).toEqual(jobs);
    expect(fetch).toHaveBeenCalledWith(
      '/api/jobs',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it('fetches repair verify previews', async () => {
    const preview = { ok: true, issue: 9, command: 'pytest', allowlisted: true, error: '' };
    vi.stubGlobal('fetch', mockFetchJson(preview));
    expect(await fetchVerifyCommand('9')).toEqual(preview);
    expect(fetch).toHaveBeenCalledWith(
      '/api/repair/verify-command?issue=9',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(await fetchVerifyCommand('9', 'boomtick')).toEqual(preview);
    expect(fetch).toHaveBeenCalledWith(
      '/api/repair/verify-command?issue=9&repo=boomtick',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it('posts repair aborts', async () => {
    vi.stubGlobal('fetch', mockFetchJson({ ok: true, killed: true }));
    expect(await abortRepair()).toEqual({ ok: true, killed: true });
    expect(fetch).toHaveBeenCalledWith(
      '/api/repair/abort',
      expect.objectContaining({ method: 'POST' }),
    );
  });

  it('fetches repo diffs (issue #663)', async () => {
    const diff = {
      repo: 'demo',
      branch: 'main',
      clean: false,
      status_short: ' M a.py',
      diff_stat: ' a.py | 2 +-',
      diff: 'diff --git a/a.py b/a.py',
      truncated: false,
    };
    vi.stubGlobal('fetch', mockFetchJson(diff));
    expect(await fetchRepoDiff('demo')).toEqual(diff);
    expect(fetch).toHaveBeenCalledWith(
      '/api/repo/diff?repo=demo',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it('posts repo actions with the confirm intent (issue #927)', async () => {
    const res = { ok: true, repo: 'demo', action: 'stash', clean: true, detail: 'changes stashed' };
    vi.stubGlobal('fetch', mockFetchJson(res));
    expect(await repoAction('demo', 'stash')).toEqual(res);
    expect(fetch).toHaveBeenCalledWith('/api/repo/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': expect.any(String) },
      body: JSON.stringify({
        repo: 'demo',
        action: 'stash',
        intent: 'repo-action',
        confirmed: true,
      }),
      signal: expect.any(AbortSignal),
    });
  });

  it('posts briefing refreshes without a confirm intent (issue #929)', async () => {
    const res = { ok: true, repo: 'demo', started: true };
    vi.stubGlobal('fetch', mockFetchJson(res));
    expect(await refreshBriefing('demo')).toEqual(res);
    expect(fetch).toHaveBeenCalledWith('/api/briefing/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': expect.any(String) },
      body: JSON.stringify({ repo: 'demo' }),
      signal: expect.any(AbortSignal),
    });
  });

  it('fetches container stats (issue #663)', async () => {
    const stats = { containers: [] };
    vi.stubGlobal('fetch', mockFetchJson(stats));
    expect(await fetchContainerStats()).toEqual(stats);
    expect(fetch).toHaveBeenCalledWith(
      '/api/system/stats',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it('fetches the container log tail (issue #928)', async () => {
    const logs = { service: 'caddy', name: 'remote-caddy-1', lines: ['a'], truncated: false };
    vi.stubGlobal('fetch', mockFetchJson(logs));
    expect(await fetchContainerLogs('caddy')).toEqual(logs);
    expect(fetch).toHaveBeenCalledWith(
      '/api/system/containers/caddy/logs?tail=100',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it('encodes the service name in the log tail URL (issue #928)', async () => {
    vi.stubGlobal('fetch', mockFetchJson({ service: 'x', name: 'n', lines: [], truncated: false }));
    await fetchContainerLogs('a/b', 5);
    expect(fetch).toHaveBeenCalledWith(
      '/api/system/containers/a%2Fb/logs?tail=5',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it('restarts a container with the confirm intent (issue #928)', async () => {
    const res = { ok: true, service: 'caddy' };
    vi.stubGlobal('fetch', mockFetchJson(res));
    expect(await restartContainer('caddy')).toEqual(res);
    expect(fetch).toHaveBeenCalledWith(
      '/api/system/containers/caddy/restart',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ intent: 'container-restart', confirmed: true }),
      }),
    );
  });

  it('posts run cancels with an empty body (issue #589)', async () => {
    const res = { ok: true, id: 'abc123def456' };
    vi.stubGlobal('fetch', mockFetchJson(res));
    expect(await cancelJobRun('abc123def456')).toEqual(res);
    expect(fetch).toHaveBeenCalledWith(
      '/api/jobs/runs/abc123def456/cancel',
      expect.objectContaining({ method: 'POST', body: '{}' }),
    );
  });

  it('fetches notification status (issue #615)', async () => {
    const status = { ntfy_configured: true, last_digest: 5 };
    vi.stubGlobal('fetch', mockFetchJson(status));
    expect(await fetchNotificationStatus()).toEqual(status);
    expect(fetch).toHaveBeenCalledWith(
      '/api/notifications/status',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
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

  it('fetches the digest schedule (issue #636)', async () => {
    const sched = { cadence: 'daily', next_run: 9, last_sent: null, last_result: {} };
    vi.stubGlobal('fetch', mockFetchJson(sched));
    expect(await fetchDigestSchedule()).toEqual(sched);
    expect(fetch).toHaveBeenCalledWith(
      '/api/notifications/digest-schedule',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it('sets digest cadence (issue #636)', async () => {
    const sched = { cadence: 'weekly', next_run: 9, last_sent: null, last_result: {} };
    vi.stubGlobal('fetch', mockFetchJson(sched));
    expect(await setDigestSchedule('weekly')).toEqual(sched);
    expect(fetch).toHaveBeenCalledWith(
      '/api/notifications/digest-schedule',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ cadence: 'weekly' }) }),
    );
  });

  it('fetches notification preferences (issue #656)', async () => {
    const prefs = { muted_events: [], events: ['ci_fail'] };
    vi.stubGlobal('fetch', mockFetchJson(prefs));
    expect(await fetchNotificationPreferences()).toEqual(prefs);
    expect(fetch).toHaveBeenCalledWith(
      '/api/notifications/preferences',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it('saves notification preferences partially (issue #656)', async () => {
    const prefs = { muted_events: ['ci_fail'], events: ['ci_fail'] };
    vi.stubGlobal('fetch', mockFetchJson(prefs));
    expect(await saveNotificationPreferences({ muted_events: ['ci_fail'] })).toEqual(prefs);
    expect(fetch).toHaveBeenCalledWith(
      '/api/notifications/preferences',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ muted_events: ['ci_fail'] }) }),
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
    expect(fetch).toHaveBeenCalledWith(
      '/api/ledger?kind=all&limit=10',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it('passes since for windowed ledger reads (issue #609)', async () => {
    const ledger = { entries: [] };
    vi.stubGlobal('fetch', mockFetchJson(ledger));
    expect(await fetchLedger('all', 200, 123.5)).toEqual(ledger);
    expect(fetch).toHaveBeenCalledWith(
      '/api/ledger?kind=all&limit=200&since=123.5',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it('passes source for ledger union reads (issue #590)', async () => {
    const ledger = { entries: [] };
    vi.stubGlobal('fetch', mockFetchJson(ledger));
    expect(await fetchLedger('all', 50, undefined, 'cli')).toEqual(ledger);
    expect(fetch).toHaveBeenCalledWith(
      '/api/ledger?kind=all&limit=50&source=cli',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it('fetches system health', async () => {
    const sys = { disk: {}, memory: {}, containers: [] };
    vi.stubGlobal('fetch', mockFetchJson(sys));
    expect(await fetchSystemHealth()).toEqual(sys);
    expect(fetch).toHaveBeenCalledWith(
      '/api/system/health',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it('fetches workflow health', async () => {
    const health = { repo: 'demo', status: 'ok', total_failing: 0, workflows: [] };
    vi.stubGlobal('fetch', mockFetchJson(health));
    expect(await fetchWorkflowHealth('demo')).toEqual(health);
    expect(fetch).toHaveBeenCalledWith(
      '/api/workflow-health?repo=demo',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it('throws visibly on HTTP errors (no silent fallback)', async () => {
    vi.stubGlobal('fetch', mockFetchJson({ detail: 'boom' }, false, 500));
    await expect(fetchJobs()).rejects.toThrow('GET api/jobs failed: 500');
  });

  it('notifies failure subscribers on HTTP errors, not on success (issue #588)', async () => {
    const seen: number[] = [];
    const unsubscribe = subscribeApiFailures(() => seen.push(1));
    try {
      vi.stubGlobal('fetch', mockFetchJson({ detail: 'boom' }, false, 502));
      await expect(fetchAttention('remote-server')).rejects.toThrow('GET api/attention');
      expect(seen).toHaveLength(1);
      vi.stubGlobal('fetch', mockFetchJson({ updated_at: 1, items: [] }));
      await fetchAttention('remote-server');
      expect(seen).toHaveLength(1);
    } finally {
      unsubscribe();
    }
  });

  it('notifies failure subscribers on POST errors and network failures (issue #588)', async () => {
    const seen: number[] = [];
    const unsubscribe = subscribeApiFailures(() => seen.push(1));
    try {
      vi.stubGlobal('fetch', mockFetchJson({ detail: 'boom' }, false, 500));
      await expect(triggerDigest()).rejects.toThrow('POST api/notifications/trigger-digest');
      expect(seen).toHaveLength(1);
      vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('network down')));
      await expect(fetchAttention('remote-server')).rejects.toThrow('network down');
      expect(seen).toHaveLength(2);
    } finally {
      unsubscribe();
    }
  });

  it('stops notifying after unsubscribe (issue #588)', async () => {
    const seen: number[] = [];
    const unsubscribe = subscribeApiFailures(() => seen.push(1));
    unsubscribe();
    vi.stubGlobal('fetch', mockFetchJson({ detail: 'boom' }, false, 500));
    await expect(fetchJobs()).rejects.toThrow('GET api/jobs failed: 500');
    expect(seen).toHaveLength(0);
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
    expect(fetch).toHaveBeenCalledWith(
      '/api/inbox?repo=remote-server',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it('fetches the pr diff for a repo and number', async () => {
    vi.stubGlobal('fetch', mockFetchJson({ number: 1095, diff: 'x', truncated: false }));
    await fetchPrDiff('boomtick', 1095);
    expect(fetch).toHaveBeenCalledWith(
      '/api/pr-diff?repo=boomtick&pr=1095',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it('absolutizes api paths from the Vite base, passes the rest through', () => {
    expect(apiUrl('api/jobs')).toBe('/api/jobs');
    expect(apiUrl('/api/jobs')).toBe('/api/jobs');
    expect(apiUrl('https://example.com/x')).toBe('https://example.com/x');
  });

  it('fetches the agent provider allowlist', async () => {
    vi.stubGlobal('fetch', mockFetchJson({ providers: ['muse', 'agy'] }));
    expect(await fetchAgentProviders()).toEqual({ providers: ['muse', 'agy'] });
    expect(fetch).toHaveBeenCalledWith(
      '/api/agent/providers',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
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
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': expect.any(String) },
      body: JSON.stringify({ session_id: 'abc123' }),
      signal: expect.any(AbortSignal),
    });
  });

  it('createIssue passes labels through to the backend', async () => {
    const created = { ok: true, url: 'https://example.test/issues/9' };
    vi.stubGlobal('fetch', mockFetchJson(created));
    expect(await createIssue('remote-server', 't', 'b', ['ready', 'bug'])).toEqual(created);
    expect(fetch).toHaveBeenCalledWith('/api/create-issue', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': expect.any(String) },
      body: JSON.stringify({ repo: 'remote-server', title: 't', body: 'b', labels: ['ready', 'bug'] }),
      signal: expect.any(AbortSignal),
    });
  });

  it('createIssue omits labels when none are given', async () => {
    const created = { ok: true, url: 'https://example.test/issues/9' };
    vi.stubGlobal('fetch', mockFetchJson(created));
    await createIssue('remote-server', 't', 'b');
    expect(fetch).toHaveBeenCalledWith('/api/create-issue', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': expect.any(String) },
      body: JSON.stringify({ repo: 'remote-server', title: 't', body: 'b' }),
      signal: expect.any(AbortSignal),
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

  it('invalidation during flight wins: the stale settle does not re-cache (issue #511)', async () => {
    let resolveStale!: (resp: unknown) => void;
    const gated = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveStale = resolve;
        }),
    );
    vi.stubGlobal('fetch', gated);
    const stale = fetchJobs();
    expect(gated).toHaveBeenCalledTimes(1);
    invalidateViewCaches();
    const freshBody = { jobs: [{ id: 'fresh' }] };
    const fresh = mockFetchJson(freshBody);
    vi.stubGlobal('fetch', fresh);
    resolveStale({
      ok: true,
      status: 200,
      json: async () => jobsBody,
      text: async () => '',
    });
    // The in-flight caller still gets its own response; it just is
    // not written back to the cache.
    await expect(stale).resolves.toEqual(jobsBody);
    // A fresh fetch after the invalidation serves live data, and the
    // stale settle left nothing behind: the retry hits the fresh
    // cache entry instead of refetching.
    await expect(fetchJobs()).resolves.toEqual(freshBody);
    expect(fresh).toHaveBeenCalledTimes(1);
    await fetchJobs();
    expect(fresh).toHaveBeenCalledTimes(1);
  });

  it('a refetch started after invalidation survives the stale settle (issue #511)', async () => {
    let resolveStale!: (resp: unknown) => void;
    const gated = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveStale = resolve;
        }),
    );
    vi.stubGlobal('fetch', gated);
    const stale = fetchJobs();
    invalidateViewCaches();
    let resolveFresh!: (resp: unknown) => void;
    const freshFetch = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveFresh = resolve;
        }),
    );
    vi.stubGlobal('fetch', freshFetch);
    const freshBody = { jobs: [{ id: 'fresh' }] };
    const freshResp = {
      ok: true,
      status: 200,
      json: async () => freshBody,
      text: async () => '',
    };
    const a = fetchJobs();
    const b = fetchJobs();
    resolveStale({
      ok: true,
      status: 200,
      json: async () => jobsBody,
      text: async () => '',
    });
    await expect(stale).resolves.toEqual(jobsBody);
    // The stale settle must not evict the newer in-flight fetch: a
    // third caller joins it instead of starting another request.
    const c = fetchJobs();
    resolveFresh(freshResp);
    await expect(a).resolves.toEqual(freshBody);
    await expect(b).resolves.toEqual(freshBody);
    await expect(c).resolves.toEqual(freshBody);
    expect(freshFetch).toHaveBeenCalledTimes(1);
    await fetchJobs();
    expect(freshFetch).toHaveBeenCalledTimes(1);
  });
});

describe('chat stream post (issue #459)', () => {
  it('streams via POST with the prompt in the body, never the URL', async () => {
    const resp = { ok: true, status: 200 };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(resp));
    expect(await postChatStream('hi', 'r', 's')).toBe(resp);
    expect(fetch).toHaveBeenCalledWith('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt: 'hi', repo: 'r', session_id: 's', prefer: 'auto' }),
      signal: undefined,
    });
  });

  it('carries the backend prefer flag and caller abort signal', async () => {
    const resp = { ok: true, status: 200 };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(resp));
    const ctrl = new AbortController();
    await postChatStream('secret @plan.md contents', 'r', 's', 'cloud', ctrl.signal);
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe('/api/chat/stream');
    expect(String(url)).not.toContain('prompt');
    expect(String(url)).not.toContain('secret');
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({
      prompt: 'secret @plan.md contents',
      repo: 'r',
      session_id: 's',
      prefer: 'cloud',
    });
    expect((init as RequestInit).signal).toBe(ctrl.signal);
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

  it('never carries mentions or prompt text (issue #653, cf. #459)', async () => {
    const body = { ok: true, provider: 'p', model: 'm', metered: false };
    const fetchMock = mockFetchJson(body);
    vi.stubGlobal('fetch', fetchMock);
    await fetchRouteHint('auto');
    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toBe('/api/chat/route-hint?prefer=auto');
    expect(url).not.toContain('@');
    expect(url).not.toContain('prompt');
  });
});

describe('repo files for @-mention completion (issue #653)', () => {
  it('fetches paths with repo and prefix', async () => {
    const body = {
      repo: 'demo',
      prefix: 'sub/',
      files: ['sub/a.py'],
      truncated: false,
    };
    vi.stubGlobal('fetch', mockFetchJson(body));
    expect(await fetchRepoFiles('demo', 'sub/')).toEqual(body);
    expect(fetch).toHaveBeenCalledWith(
      '/api/repo/files?repo=demo&prefix=sub%2F',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it('defaults to an empty prefix', async () => {
    const body = { repo: 'demo', prefix: '', files: [], truncated: false };
    vi.stubGlobal('fetch', mockFetchJson(body));
    expect(await fetchRepoFiles('demo')).toEqual(body);
    expect(fetch).toHaveBeenCalledWith(
      '/api/repo/files?repo=demo&prefix=',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
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

  it('drops malformed jobs rows instead of casting them', () => {
    const seen: FakeSource[] = [];
    const jobs: unknown[] = [];
    const live: boolean[] = [];
    subscribeJobsPush(
      {
        onJobs: (j) => jobs.push(j),
        onLive: (l) => live.push(l),
      },
      makeFactory(seen),
    );
    seen[0].emit(
      'jobs',
      JSON.stringify({ jobs: [{ id: 'a' }, 42, null, { noid: 1 }] }),
    );
    expect(jobs).toEqual([[{ id: 'a' }]]);
    expect(live).toEqual([true]);
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
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': expect.any(String) },
      body: JSON.stringify({
        repo: 'remote-server',
        pr_number: 7,
        action: 'approve',
        intent: 'pr-approve',
        confirmed: true,
      }),
      signal: expect.any(AbortSignal),
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
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': expect.any(String) },
      body: JSON.stringify({
        repo: 'remote-server',
        pr_number: 7,
        action: 'request-changes',
        body: 'Fix the leak.',
        intent: 'pr-request-changes',
        confirmed: true,
      }),
      signal: expect.any(AbortSignal),
    });
  });

  it('surfaces endpoint errors to the row', async () => {
    vi.stubGlobal('fetch', mockFetchJson({ detail: 'nope' }, false, 400));
    await expect(approvePr('remote-server', 7)).rejects.toThrow('POST api/pr-action failed: 400');
  });
});

describe('job re-run (issue #630)', () => {
  it('replays td argv to run-td joined as a command', async () => {
    vi.stubGlobal('fetch', mockFetchJson({ id: 'abc123def456' }));
    expect(await rerunJob('run-td', 'remote-server', ['td', '--json', 'preflight'], false)).toEqual({
      id: 'abc123def456',
    });
    expect(fetch).toHaveBeenCalledWith('/api/jobs/run-td', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': expect.any(String) },
      body: JSON.stringify({ command: 'td --json preflight', repo: 'remote-server' }),
      signal: expect.any(AbortSignal),
    });
  });

  it('replays plain actions to run with the confirm intent when destructive', async () => {
    vi.stubGlobal('fetch', mockFetchJson({ id: 'abc123def456' }));
    await rerunJob('recreate-code', 'remote-server', [], true);
    expect(fetch).toHaveBeenCalledWith('/api/jobs/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': expect.any(String) },
      body: JSON.stringify({
        action: 'recreate-code',
        repo: 'remote-server',
        intent: 'jobs-run',
        confirmed: true,
      }),
      signal: expect.any(AbortSignal),
    });
  });

  it('replays safe plain actions without a confirm payload', async () => {
    vi.stubGlobal('fetch', mockFetchJson({ id: 'abc123def456' }));
    await rerunJob('logs', 'remote-server', [], false);
    expect(fetch).toHaveBeenCalledWith('/api/jobs/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': expect.any(String) },
      body: JSON.stringify({ action: 'logs', repo: 'remote-server' }),
      signal: expect.any(AbortSignal),
    });
  });

  it('surfaces single-flight conflicts to the caller', async () => {
    vi.stubGlobal('fetch', mockFetchJson({ detail: 'x' }, false, 409));
    await expect(rerunJob('logs', 'remote-server', [], false)).rejects.toThrow(
      'POST api/jobs/run failed: 409',
    );
  });
});

describe('explicit action branch (issue #708)', () => {
  const postHeaders = {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': expect.any(String) },
  };

  it('sends the branch with merge and approve', async () => {
    vi.stubGlobal('fetch', mockFetchJson({ ok: true, output: 'merged' }));
    await mergePr('remote-server', 7, 'main');
    expect(fetch).toHaveBeenCalledWith('/api/pr-action', {
      ...postHeaders,
      body: JSON.stringify({
        repo: 'remote-server',
        pr_number: 7,
        action: 'merge',
        intent: 'pr-merge',
        confirmed: true,
        branch: 'main',
      }),
      signal: expect.any(AbortSignal),
    });
    await approvePr('remote-server', 7, 'feat/x');
    expect(fetch).toHaveBeenCalledWith('/api/pr-action', {
      ...postHeaders,
      body: JSON.stringify({
        repo: 'remote-server',
        pr_number: 7,
        action: 'approve',
        intent: 'pr-approve',
        confirmed: true,
        branch: 'feat/x',
      }),
      signal: expect.any(AbortSignal),
    });
  });

  it('omits a blank branch so the payload matches today', async () => {
    vi.stubGlobal('fetch', mockFetchJson({ ok: true, output: 'approved' }));
    await approvePr('remote-server', 7, '  ');
    expect(fetch).toHaveBeenCalledWith('/api/pr-action', {
      ...postHeaders,
      body: JSON.stringify({
        repo: 'remote-server',
        pr_number: 7,
        action: 'approve',
        intent: 'pr-approve',
        confirmed: true,
      }),
      signal: expect.any(AbortSignal),
    });
  });

  it('sends the branch with job and td launches', async () => {
    vi.stubGlobal('fetch', mockFetchJson({ id: 'abc123def456' }));
    await startJobRun('logs', 'remote-server', false, 'main');
    expect(fetch).toHaveBeenCalledWith('/api/jobs/run', {
      ...postHeaders,
      body: JSON.stringify({ action: 'logs', repo: 'remote-server', branch: 'main' }),
      signal: expect.any(AbortSignal),
    });
    await startTdRun('td context-warm', 'remote-server', 'feat/x');
    expect(fetch).toHaveBeenCalledWith('/api/jobs/run-td', {
      ...postHeaders,
      body: JSON.stringify({
        command: 'td context-warm',
        repo: 'remote-server',
        branch: 'feat/x',
      }),
      signal: expect.any(AbortSignal),
    });
  });

  it('passes the branch through re-runs', async () => {
    vi.stubGlobal('fetch', mockFetchJson({ id: 'abc123def456' }));
    await rerunJob('run-td', 'remote-server', ['td', 'context-warm'], false, 'main');
    expect(fetch).toHaveBeenCalledWith('/api/jobs/run-td', {
      ...postHeaders,
      body: JSON.stringify({
        command: 'td context-warm',
        repo: 'remote-server',
        branch: 'main',
      }),
      signal: expect.any(AbortSignal),
    });
    await rerunJob('logs', 'remote-server', [], false, 'main');
    expect(fetch).toHaveBeenCalledWith('/api/jobs/run', {
      ...postHeaders,
      body: JSON.stringify({ action: 'logs', repo: 'remote-server', branch: 'main' }),
      signal: expect.any(AbortSignal),
    });
  });

  it('sends the branch with repair dispatch, omits when blank', async () => {
    vi.stubGlobal('fetch', mockFetchJson({ ok: true, issue: 9, status: 'running', repo: 'remote-server' }));
    await dispatchRepair('9', 'main');
    expect(fetch).toHaveBeenCalledWith('/api/repair/dispatch', {
      ...postHeaders,
      body: JSON.stringify({ issue: '9', intent: 'repair-dispatch', confirmed: true, branch: 'main' }),
      signal: expect.any(AbortSignal),
    });
    await dispatchRepair('9', '');
    expect(fetch).toHaveBeenCalledWith('/api/repair/dispatch', {
      ...postHeaders,
      body: JSON.stringify({ issue: '9', intent: 'repair-dispatch', confirmed: true }),
      signal: expect.any(AbortSignal),
    });
  });

  it('scopes repair dispatch to the repo (issue #435)', async () => {
    vi.stubGlobal('fetch', mockFetchJson({ ok: true, issue: 5, status: 'running', repo: 'boomtick' }));
    await dispatchRepair('5', 'main', 'boomtick');
    expect(fetch).toHaveBeenCalledWith('/api/repair/dispatch', {
      ...postHeaders,
      body: JSON.stringify({
        issue: '5',
        repo: 'boomtick',
        intent: 'repair-dispatch',
        confirmed: true,
        branch: 'main',
      }),
      signal: expect.any(AbortSignal),
    });
  });
});

describe('run compare (issue #657)', () => {
  it('fetches the unified diff for two run ids', async () => {
    const body = {
      a_id: 'aaaaaaaaaaaa',
      b_id: 'bbbbbbbbbbbb',
      unified_diff: '--- a/aaaaaaaaaaaa\n+++ b/bbbbbbbbbbbb\n@@ -1 +1 @@\n-x\n+y\n',
      truncated: false,
    };
    vi.stubGlobal('fetch', mockFetchJson(body));
    expect(await fetchRunCompare('aaaaaaaaaaaa', 'bbbbbbbbbbbb')).toEqual(body);
    expect(fetch).toHaveBeenCalledWith(
      '/api/jobs/runs/compare?a=aaaaaaaaaaaa&b=bbbbbbbbbbbb',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it('surfaces unknown-run 404s to the caller', async () => {
    vi.stubGlobal('fetch', mockFetchJson({ detail: 'unknown run' }, false, 404));
    await expect(fetchRunCompare('aaaaaaaaaaaa', 'bbbbbbbbbbbb')).rejects.toThrow(
      'failed: 404',
    );
  });
});

describe('offline outbox (issue #404)', () => {
  function goOffline() {
    vi.stubGlobal('navigator', { onLine: false });
  }

  function sentKey(init: unknown): string {
    const headers = (init as { headers: Record<string, string> }).headers;
    return headers['Idempotency-Key'];
  }

  function stubLocalStorage(seed: Record<string, string> = {}) {
    const store = new Map<string, string>(Object.entries(seed));
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => (store.has(k) ? (store.get(k) as string) : null),
      setItem: (k: string, v: string) => {
        store.set(k, v);
      },
      removeItem: (k: string) => {
        store.delete(k);
      },
    });
    return store;
  }

  it('sends an idempotency key with every mutating POST', async () => {
    const fetchMock = mockFetchJson({ ok: true, killed: true });
    vi.stubGlobal('fetch', fetchMock);
    await abortAgentSession('abc123');
    const key = sentKey(fetchMock.mock.calls[0][1]);
    expect(typeof key).toBe('string');
    expect(key.length).toBeGreaterThan(0);
  });

  it('mints a fresh key per call', async () => {
    const fetchMock = mockFetchJson({ ok: true, killed: true });
    vi.stubGlobal('fetch', fetchMock);
    await abortAgentSession('a');
    await abortAgentSession('b');
    const keys = fetchMock.mock.calls.map((call) => sentKey(call[1]));
    expect(keys[0]).not.toBe(keys[1]);
  });

  it('queues offline taps instead of fetching', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    goOffline();
    await expect(startJobRun('logs', 'remote-server', false)).rejects.toThrow(
      'POST api/jobs/run queued offline (1 pending)',
    );
    expect(fetchMock).not.toHaveBeenCalled();
    const queue = getOutbox();
    expect(queue).toHaveLength(1);
    expect(queue[0].method).toBe('POST');
    expect(queue[0].path).toBe('api/jobs/run');
    expect(queue[0].body).toEqual({ action: 'logs', repo: 'remote-server' });
    expect(queue[0].key.length).toBeGreaterThan(0);
  });

  it('marks queued failures so callers can tell them apart', async () => {
    vi.stubGlobal('fetch', vi.fn());
    goOffline();
    let err: unknown;
    try {
      await startJobRun('logs', 'remote-server', false);
    } catch (e) {
      err = e;
    }
    expect(err).toBeInstanceOf(QueuedOfflineError);
    expect((err as QueuedOfflineError).queued).toBe(true);
    expect((err as QueuedOfflineError).entry.path).toBe('api/jobs/run');
  });

  it('queues when the connection drops mid-request', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    await expect(snoozeAttention('item-1', 30)).rejects.toThrow('queued offline');
    expect(getOutbox()).toHaveLength(1);
    expect(getOutbox()[0].path).toBe('api/attention/snooze');
  });

  it('never queues HTTP errors — the server answered', async () => {
    vi.stubGlobal('fetch', mockFetchJson({ detail: 'nope' }, false, 500));
    await expect(triggerDigest()).rejects.toThrow(
      'POST api/notifications/trigger-digest failed: 500',
    );
    expect(getOutbox()).toHaveLength(0);
  });

  it('never queues reads — they fail as today', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('network down')));
    await expect(fetchAttention('remote-server')).rejects.toThrow('network down');
    expect(getOutbox()).toHaveLength(0);
  });

  it('replays the queue with the stored keys and drains without dupes', async () => {
    vi.stubGlobal('fetch', vi.fn());
    goOffline();
    await expect(startJobRun('logs', 'remote-server', false)).rejects.toThrow('queued offline');
    await expect(snoozeAttention('item-1', 30)).rejects.toThrow('queued offline');
    const [first, second] = getOutbox();
    expect(getOutbox()).toHaveLength(2);
    vi.unstubAllGlobals();
    const fetchMock = mockFetchJson({ ok: true });
    vi.stubGlobal('fetch', fetchMock);
    const result = await flushOutbox();
    expect(result).toEqual({ synced: 2, failed: 0, pending: 0 });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(sentKey(fetchMock.mock.calls[0][1])).toBe(first.key);
    expect(sentKey(fetchMock.mock.calls[1][1])).toBe(second.key);
    expect(getOutbox()).toHaveLength(0);
    await flushOutbox();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('stops the run and keeps the rest when connectivity drops mid-flush', async () => {
    vi.stubGlobal('fetch', vi.fn());
    goOffline();
    await expect(startJobRun('logs', 'remote-server', false)).rejects.toThrow('queued offline');
    await expect(snoozeAttention('item-1', 30)).rejects.toThrow('queued offline');
    vi.unstubAllGlobals();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({}),
        text: async () => '',
      })
      .mockRejectedValueOnce(new TypeError('Failed to fetch'));
    vi.stubGlobal('fetch', fetchMock);
    const result = await flushOutbox();
    expect(result.synced).toBe(1);
    expect(result.failed).toBe(0);
    expect(result.pending).toBe(1);
    expect(getOutbox()).toHaveLength(1);
    expect(getOutbox()[0].path).toBe('api/attention/snooze');
  });

  it('keeps server-rejected entries and reports the failure for retry', async () => {
    vi.stubGlobal('fetch', vi.fn());
    goOffline();
    await expect(startJobRun('logs', 'remote-server', false)).rejects.toThrow('queued offline');
    vi.unstubAllGlobals();
    vi.stubGlobal('fetch', mockFetchJson({ detail: 'nope' }, false, 500));
    const result = await flushOutbox();
    expect(result).toEqual({ synced: 0, failed: 1, pending: 1 });
    expect(getOutbox()).toHaveLength(1);
    expect(getOutboxStatus().lastError).toContain('POST api/jobs/run failed: 500');
    vi.stubGlobal('fetch', mockFetchJson({ id: 'abc123def456' }));
    const retry = await flushOutbox();
    expect(retry).toEqual({ synced: 1, failed: 0, pending: 0 });
    expect(getOutboxStatus().lastError).toBeNull();
  });

  it('persists the queue to localStorage', async () => {
    const store = stubLocalStorage();
    vi.stubGlobal('fetch', vi.fn());
    goOffline();
    await expect(startJobRun('logs', 'remote-server', false)).rejects.toThrow('queued offline');
    const raw = store.get('buttons.outbox.v1');
    expect(raw).toContain('api/jobs/run');
    expect(getOutbox()).toHaveLength(1);
  });

  it('reads a queue persisted by an earlier session', async () => {
    stubLocalStorage({
      'buttons.outbox.v1': JSON.stringify([
        {
          key: 'seeded-key',
          method: 'POST',
          path: 'api/jobs/run',
          body: { action: 'logs', repo: 'remote-server' },
          queuedAt: 1,
        },
      ]),
    });
    const fetchMock = mockFetchJson({ id: 'abc123def456' });
    vi.stubGlobal('fetch', fetchMock);
    expect(getOutbox()).toHaveLength(1);
    await flushOutbox();
    expect(sentKey(fetchMock.mock.calls[0][1])).toBe('seeded-key');
    expect(getOutbox()).toHaveLength(0);
  });

  it('ignores a corrupt persisted queue instead of crashing', () => {
    stubLocalStorage({ 'buttons.outbox.v1': 'not-json{{' });
    expect(getOutbox()).toEqual([]);
  });

  it('notifies outbox subscribers on enqueue and sync', async () => {
    let calls = 0;
    const unsubscribe = subscribeOutbox(() => {
      calls += 1;
    });
    try {
      vi.stubGlobal('fetch', vi.fn());
      goOffline();
      await expect(startJobRun('logs', 'remote-server', false)).rejects.toThrow(
        'queued offline',
      );
      expect(calls).toBeGreaterThanOrEqual(1);
      const afterEnqueue = calls;
      vi.unstubAllGlobals();
      vi.stubGlobal('fetch', mockFetchJson({ id: 'abc123def456' }));
      await flushOutbox();
      expect(calls).toBeGreaterThan(afterEnqueue);
    } finally {
      unsubscribe();
    }
  });

  it('queues DELETE mutations too', async () => {
    vi.stubGlobal('fetch', vi.fn());
    goOffline();
    await expect(restoreRow('remote-server', 'pr', 7)).rejects.toThrow(
      'DELETE api/deferred queued offline',
    );
    expect(getOutbox()).toHaveLength(1);
    expect(getOutbox()[0].method).toBe('DELETE');
  });

  it('createIssue queues offline and keeps its error shape online', async () => {
    vi.stubGlobal('fetch', vi.fn());
    goOffline();
    await expect(createIssue('remote-server', 't', 'b')).rejects.toThrow('queued offline');
    expect(getOutbox()).toHaveLength(1);
    vi.unstubAllGlobals();
    vi.stubGlobal('fetch', mockFetchJson({ detail: 'nope' }, false, 500));
    await expect(createIssue('remote-server', 't', 'b')).rejects.toThrow(
      'POST /api/create-issue failed: 500',
    );
  });

  it('clearOutbox drops the queue and the last error', async () => {
    vi.stubGlobal('fetch', vi.fn());
    goOffline();
    await expect(startJobRun('logs', 'remote-server', false)).rejects.toThrow('queued offline');
    clearOutbox();
    expect(getOutbox()).toHaveLength(0);
    expect(getOutboxStatus()).toEqual({ online: false, pending: 0, syncing: false, lastError: null });
  });
});

describe('request timeouts (issue #460)', () => {
  // Mimics real fetch against a wedged backend: pending until the
  // signal aborts, then rejects with the abort reason.
  function mockHungFetch() {
    return vi.fn().mockImplementation((_url: unknown, init?: RequestInit) => new Promise(
      (_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => {
          reject(init?.signal?.reason ?? new DOMException('aborted', 'AbortError'));
        });
      },
    ));
  }

  it('fails a hung GET within its budget', async () => {
    vi.useFakeTimers();
    try {
      vi.stubGlobal('fetch', mockHungFetch());
      const pending = fetchJobs();
      const assertion = expect(pending).rejects.toBeInstanceOf(RequestTimeoutError);
      await vi.advanceTimersByTimeAsync(API_TIMEOUT_MS);
      await assertion;
      const caught: unknown = await pending.catch((e: unknown) => e);
      const err = caught as RequestTimeoutError;
      expect(err.message).toMatch(/timeout/i);
      expect(plainLanguage(err.message, 'Job')).toBe(
        'Network request failed — the service could not be reached',
      );
      expect(err.path).toBe('api/jobs');
      expect(err.budgetMs).toBe(API_TIMEOUT_MS);
    } finally {
      vi.useRealTimers();
    }
  });

  it('gives briefing the slow budget', async () => {
    vi.useFakeTimers();
    try {
      vi.stubGlobal('fetch', mockHungFetch());
      const pending = fetchBriefing('remote-server');
      let settled = false;
      pending.then(
        () => { settled = true; },
        () => { settled = true; },
      );
      await vi.advanceTimersByTimeAsync(API_TIMEOUT_MS);
      expect(settled).toBe(false);
      const assertion = expect(pending).rejects.toBeInstanceOf(RequestTimeoutError);
      await vi.advanceTimersByTimeAsync(API_SLOW_TIMEOUT_MS - API_TIMEOUT_MS);
      await assertion;
    } finally {
      vi.useRealTimers();
    }
  });

  it('broadcasts timeouts to API failure listeners', async () => {
    vi.useFakeTimers();
    try {
      vi.stubGlobal('fetch', mockHungFetch());
      let calls = 0;
      const unsubscribe = subscribeApiFailures(() => { calls += 1; });
      try {
        const pending = fetchSystemHealth();
        const assertion = expect(pending).rejects.toBeInstanceOf(RequestTimeoutError);
        await vi.advanceTimersByTimeAsync(API_TIMEOUT_MS);
        await assertion;
        expect(calls).toBe(1);
      } finally {
        unsubscribe();
      }
    } finally {
      vi.useRealTimers();
    }
  });

  it('surfaces mutation timeouts instead of queuing them offline', async () => {
    vi.useFakeTimers();
    try {
      vi.stubGlobal('fetch', mockHungFetch());
      const pending = abortRepair();
      const assertion = expect(pending).rejects.toBeInstanceOf(RequestTimeoutError);
      await vi.advanceTimersByTimeAsync(API_TIMEOUT_MS);
      await assertion;
      expect(getOutbox()).toEqual([]);
    } finally {
      vi.useRealTimers();
    }
  });

  it('clears the timeout once the request settles', async () => {
    vi.useFakeTimers();
    try {
      vi.stubGlobal('fetch', mockFetchJson({ jobs: [] }));
      await fetchJobs();
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it('passes caller abort signals on streams untouched', async () => {
    const resp = { ok: true, status: 200 };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(resp));
    const ctrl = new AbortController();
    expect(await postAgentTurn('muse', 'remote-server', 'hi', 'sess1', ctrl.signal)).toBe(resp);
    expect(fetch).toHaveBeenCalledWith(
      '/api/agent/turn',
      expect.objectContaining({ signal: ctrl.signal }),
    );
  });
});

describe('auth expiry (issue #1035)', () => {
  const loginUrl = 'https://github.com/login/oauth/authorize?client_id=x';

  function mockRedirectedFetch() {
    return vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      redirected: true,
      url: loginUrl,
      json: async () => ({}),
      text: async () => '',
    });
  }

  it('flags an OAuth login redirect as auth expiry', () => {
    expect(isAuthRedirect(loginUrl, 200)).toBe(true);
    expect(isAuthRedirect('https://example.com/api/jobs', 200)).toBe(false);
    expect(isAuthRedirect('https://example.com/api/jobs', 401)).toBe(true);
  });

  it('does not cry wolf on our own oauth callback path', () => {
    expect(isAuthRedirect('https://buttons.example.com/oauth2/callback?code=x', 200)).toBe(false);
  });

  function mockResp(overrides: Record<string, unknown> = {}) {
    return {
      ok: true,
      status: 200,
      redirected: false,
      url: 'https://buttons.example.com/api/jobs',
      json: async () => ({}),
      text: async () => '',
      ...overrides,
    } as unknown as Response;
  }

  it('throwIfAuthExpired throws on 401 and clears on recovery', () => {
    expect(() => throwIfAuthExpired(mockResp({ ok: false, status: 401 }), 'api/jobs')).toThrow(
      AuthExpiredError,
    );
    expect(isAuthExpired()).toBe(true);
    expect(() => throwIfAuthExpired(mockResp(), 'api/jobs')).not.toThrow();
    expect(isAuthExpired()).toBe(false);
  });

  it('retires the flag on the next good response', async () => {
    vi.stubGlobal('fetch', mockRedirectedFetch());
    await expect(fetchJobs()).rejects.toBeInstanceOf(AuthExpiredError);
    expect(isAuthExpired()).toBe(true);
    vi.stubGlobal('fetch', mockFetchJson({ jobs: [] }));
    await fetchJobs();
    expect(isAuthExpired()).toBe(false);
  });

  it('flags 401s on raw stream responses', async () => {
    vi.stubGlobal('fetch', mockFetchJson({ error: 'unauthorized' }, false, 401));
    await expect(postChatStream('hi', 'remote-server', 'sess1')).rejects.toBeInstanceOf(
      AuthExpiredError,
    );
    expect(isAuthExpired()).toBe(true);
  });

  it('never queues expired-session mutations as offline', async () => {
    vi.stubGlobal('fetch', mockRedirectedFetch());
    await expect(startJobRun('logs', 'remote-server', false)).rejects.toBeInstanceOf(
      AuthExpiredError,
    );
    expect(getOutbox()).toHaveLength(0);
  });

  it('throws AuthExpiredError and latches the flag once', async () => {
    const seen: number[] = [];
    const unsubscribe = subscribeAuthExpired(() => {
      seen.push(1);
    });
    try {
      vi.stubGlobal('fetch', mockRedirectedFetch());
      await expect(fetchJobs()).rejects.toBeInstanceOf(AuthExpiredError);
      expect(isAuthExpired()).toBe(true);
      vi.stubGlobal('fetch', mockRedirectedFetch());
      await expect(fetchJobs()).rejects.toBeInstanceOf(AuthExpiredError);
      expect(seen).toHaveLength(1);
    } finally {
      unsubscribe();
    }
  });

  it('trips the session banner after repeated opaque network failures (issue #1143)', async () => {
    const seen: number[] = [];
    const unsubscribe = subscribeAuthExpired(() => {
      seen.push(1);
    });
    try {
      vi.stubGlobal('fetch', () => Promise.reject(new TypeError('Failed to fetch')));
      for (let i = 0; i < 4; i += 1) {
        await expect(fetchJobs()).rejects.toThrow('Failed to fetch');
        expect(isAuthExpired()).toBe(false);
      }
      await expect(fetchJobs()).rejects.toThrow('Failed to fetch');
      expect(isAuthExpired()).toBe(true);
      expect(seen).toHaveLength(1);
    } finally {
      unsubscribe();
    }
  });

  it('resets the network-failure run on any good response', async () => {
    vi.stubGlobal('fetch', () => Promise.reject(new TypeError('Failed to fetch')));
    for (let i = 0; i < 4; i += 1) {
      await expect(fetchJobs()).rejects.toThrow('Failed to fetch');
    }
    expect(isAuthExpired()).toBe(false);
    vi.stubGlobal('fetch', mockFetchJson({ jobs: [] }));
    await fetchJobs();
    __clearPrCaches();
    invalidateViewCaches();
    vi.stubGlobal('fetch', () => Promise.reject(new TypeError('Failed to fetch')));
    for (let i = 0; i < 4; i += 1) {
      await expect(fetchJobs()).rejects.toThrow('Failed to fetch');
    }
    expect(isAuthExpired()).toBe(false);
  });

  it('does not trip while the browser reports itself offline', async () => {
    vi.stubGlobal('navigator', { onLine: false });
    vi.stubGlobal('fetch', () => Promise.reject(new TypeError('Failed to fetch')));
    for (let i = 0; i < 6; i += 1) {
      await expect(fetchJobs()).rejects.toThrow('Failed to fetch');
    }
    expect(isAuthExpired()).toBe(false);
  });

  it('treats a 401 JSON body as auth expiry', async () => {
    vi.stubGlobal('fetch', mockFetchJson({ error: 'unauthorized' }, false, 401));
    await expect(fetchSystemHealth()).rejects.toBeInstanceOf(AuthExpiredError);
    expect(isAuthExpired()).toBe(true);
  });
});
