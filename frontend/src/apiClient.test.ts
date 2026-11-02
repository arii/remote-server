import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  fetchRepoStatus,
  fetchSessionDetail,
  fetchSessionList,
  fetchSystemHealth,
} from './apiClient';
import listFixture from './mocks/list.json';
import detailFixture from './mocks/detail.json';

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
});

describe('apiClient (fetch with fixture JSON)', () => {
  it('fetches the session list shape', async () => {
    vi.stubGlobal('fetch', mockFetchJson(listFixture));
    const list = await fetchSessionList();
    expect(list.sessions).toHaveLength(3);
    expect(fetch).toHaveBeenCalledWith('/api/agents/sessions');
  });

  it('fetches session detail by file', async () => {
    vi.stubGlobal('fetch', mockFetchJson(detailFixture));
    const detail = await fetchSessionDetail('bg-1234567890ab.jsonl');
    expect(detail.file).toBe('bg-1234567890ab.jsonl');
    expect(detail.turns).toHaveLength(2);
    expect(fetch).toHaveBeenCalledWith(
      '/api/agents/sessions/bg-1234567890ab.jsonl',
    );
  });

  it('fetches system health', async () => {
    const sys = { disk: {}, memory: {}, containers: [] };
    vi.stubGlobal('fetch', mockFetchJson(sys));
    expect(await fetchSystemHealth()).toEqual(sys);
    expect(fetch).toHaveBeenCalledWith('/api/system/health');
  });

  it('fetches repo status', async () => {
    const repo = { repo: 'demo', branch: 'main', short: '', clean: true };
    vi.stubGlobal('fetch', mockFetchJson(repo));
    expect(await fetchRepoStatus('demo')).toEqual(repo);
    expect(fetch).toHaveBeenCalledWith('/api/repo/status?repo=demo');
  });

  it('throws visibly on HTTP errors (no silent fallback)', async () => {
    vi.stubGlobal('fetch', mockFetchJson({ detail: 'boom' }, false, 500));
    await expect(fetchSessionList()).rejects.toThrow(
      'GET /api/agents/sessions failed: 500',
    );
  });
});
