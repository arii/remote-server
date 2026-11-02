import type { DetailResponse, ListResponse } from './types';
import type {
  BriefingResponse,
  JobsResponse,
  LedgerResponse,
} from './apiTypes';

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

export function fetchBriefing(repo: string): Promise<BriefingResponse> {
  return getJson<BriefingResponse>(
    `/api/briefing?repo=${encodeURIComponent(repo)}`,
  );
}

export function fetchJobs(): Promise<JobsResponse> {
  return getJson<JobsResponse>('/api/jobs');
}

export function clearJobs(): Promise<{ ok: boolean }> {
  return getJson<{ ok: boolean }>('/api/jobs/clear');
}

export function runJob(id: string): Promise<{ ok: boolean }> {
  return getJson<{ ok: boolean }>(`/api/jobs/run?id=${encodeURIComponent(id)}`);
}

export function fetchAttention(repo: string): Promise<import('./attentionTypes').AttentionResponse> {
  return getJson<import('./attentionTypes').AttentionResponse>(
    `/api/attention?repo=${encodeURIComponent(repo)}`,
  );
}

export async function createIssue(repo: string, title: string, body: string): Promise<{ ok: boolean; url: string }> {
  const resp = await fetch('/api/create-issue', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ repo, title, body }),
  });
  if (!resp.ok) throw new Error(`POST /api/create-issue failed: ${resp.status}`);
  return (await resp.json()) as { ok: boolean; url: string };
}

export function fetchLedger(
  kind = 'all',
  limit = 50,
): Promise<LedgerResponse> {
  return getJson<LedgerResponse>(
    `/api/ledger?kind=${encodeURIComponent(kind)}&limit=${limit}`,
  );
}

export interface WorkflowRun {
  id: number | string;
  name: string;
  branch: string;
  event: string;
  conclusion: string;
  url: string;
  diagnosed_cause: string;
  annotations?: Array<Record<string, unknown>>;
  jobs?: Array<Record<string, unknown>>;
}

export interface WorkflowHealthResponse {
  repo: string;
  status: string;
  total_failing: number;
  workflows: WorkflowRun[];
}

export function fetchWorkflowHealth(repo: string): Promise<WorkflowHealthResponse> {
  return getJson<WorkflowHealthResponse>(
    `/api/workflow-health?repo=${encodeURIComponent(repo)}`,
  );
}
