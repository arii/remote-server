import type { DetailResponse, ListResponse } from './types';

export interface SystemHealthResponse {
  disk: Record<string, unknown>;
  memory: Record<string, unknown>;
  containers: Array<Record<string, unknown>>;
}

export interface RepoStatusResponse {
  repo: string;
  branch: string;
  short: string;
  clean: boolean;
}

async function getJson<T>(path: string): Promise<T> {
  const resp = await fetch(path);
  if (!resp.ok) {
    const text = await resp.text().catch(() => '');
    throw new Error(`GET ${path} failed: ${resp.status} ${text}`.trim());
  }
  return (await resp.json()) as T;
}

export function fetchSessionList(): Promise<ListResponse> {
  return getJson<ListResponse>('/api/agents/sessions');
}

export function fetchSessionDetail(file: string): Promise<DetailResponse> {
  return getJson<DetailResponse>(
    `/api/agents/sessions/${encodeURIComponent(file)}`,
  );
}

export function fetchSystemHealth(): Promise<SystemHealthResponse> {
  return getJson<SystemHealthResponse>('/api/system/health');
}

export function fetchRepoStatus(repo: string): Promise<RepoStatusResponse> {
  return getJson<RepoStatusResponse>(
    `/api/repo/status?repo=${encodeURIComponent(repo)}`,
  );
}
