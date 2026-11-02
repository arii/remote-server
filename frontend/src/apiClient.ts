import type {
  JobsResponse,
  LedgerResponse,
} from './apiTypes';

export interface SystemHealthResponse {
  disk: Record<string, unknown>;
  memory: Record<string, unknown>;
  containers: Array<Record<string, unknown>>;
}

async function getJson<T>(path: string): Promise<T> {
  const resp = await fetch(path);
  if (!resp.ok) {
    const text = await resp.text().catch(() => '');
    throw new Error(`GET ${path} failed: ${resp.status} ${text}`.trim());
  }
  return (await resp.json()) as T;
}

export function fetchSystemHealth(): Promise<SystemHealthResponse> {
  return getJson<SystemHealthResponse>('/api/system/health');
}

export function fetchJobs(): Promise<JobsResponse> {
  return getJson<JobsResponse>('/api/jobs');
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

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const resp = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error(`POST ${path} failed: ${resp.status}`);
  return (await resp.json()) as T;
}

export function fetchRepos(): Promise<import('./reviewTypes').ReposResponse> {
  return getJson<import('./reviewTypes').ReposResponse>('/api/repos');
}

export function fetchPrs(repo: string): Promise<import('./reviewTypes').PrListResponse> {
  return getJson<import('./reviewTypes').PrListResponse>(
    `/api/prs?repo=${encodeURIComponent(repo)}`,
  );
}

export function fetchPrDetails(repo: string, pr: number): Promise<import('./reviewTypes').PrDetailsResponse> {
  return getJson<import('./reviewTypes').PrDetailsResponse>(
    `/api/pr-details?repo=${encodeURIComponent(repo)}&pr=${pr}`,
  );
}

export function mergePr(repo: string, prNumber: number): Promise<{ ok: boolean; output: string }> {
  return postJson<{ ok: boolean; output: string }>('/api/pr-action', {
    repo,
    pr_number: prNumber,
    action: 'merge',
    intent: 'pr-merge',
    confirmed: true,
  });
}

export function fetchIssues(repo: string): Promise<import('./reviewTypes').IssueListResponse> {
  return getJson<import('./reviewTypes').IssueListResponse>(
    `/api/issues?repo=${encodeURIComponent(repo)}`,
  );
}

export function assignIssue(repo: string, issue: string, assignee: string): Promise<{ ok: boolean }> {
  return postJson<{ ok: boolean }>('/api/assign-issue', { repo, issue, assignee });
}

export function draftIssue(repo: string, title: string, cause: string, details: string): Promise<import('./reviewTypes').IssueDraftResponse> {
  return postJson<import('./reviewTypes').IssueDraftResponse>('/api/issue-draft', {
    repo,
    source: 'finding',
    title,
    cause,
    details,
  });
}
