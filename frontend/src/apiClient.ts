import type {
  JobsResponse,
  LedgerResponse,
} from './apiTypes';

export interface SystemHealthResponse {
  disk: Record<string, unknown>;
  memory: Record<string, unknown>;
  containers: Array<Record<string, unknown>>;
}

async function readJson<T>(resp: Response, method: string, path: string): Promise<T> {
  if (!resp.ok) {
    const text = await resp.text().catch(() => '');
    throw new Error(`${method} ${path} failed: ${resp.status} ${text}`.trim());
  }
  return (await resp.json()) as T;
}

async function getJson<T>(path: string): Promise<T> {
  return readJson<T>(await fetch(path), 'GET', path);
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

export interface PiProgressResponse {
  done: boolean;
  output: string;
  result?: Record<string, unknown> | null;
  error?: string | null;
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const resp = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return readJson<T>(resp, 'POST', path);
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

export function startJobRun(action: string, repo: string): Promise<{ id: string }> {
  return postJson<{ id: string }>('/api/jobs/run', { action, repo });
}

export function clearJobs(all: boolean): Promise<{ ok: boolean; cleared: number }> {
  return postJson<{ ok: boolean; cleared: number }>('/api/jobs/clear', {
    all,
    intent: 'jobs-clear',
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

export function fetchPiProgress(rid: string): Promise<PiProgressResponse> {
  return getJson<PiProgressResponse>(`/api/pi-progress/${encodeURIComponent(rid)}`);
}

export function chatStreamUrl(prompt: string, repo: string, sessionId: string): string {
  const q = new URLSearchParams({ prompt, repo, session_id: sessionId });
  return `/api/chat/stream?${q.toString()}`;
}

export function abortChat(sessionId: string): Promise<{ ok: boolean; session_id: string }> {
  return postJson<{ ok: boolean; session_id: string }>('/api/chat/abort', {
    session_id: sessionId,
  });
}

export function requestTakeover(): Promise<{ ok: boolean; output: string }> {
  return postJson<{ ok: boolean; output: string }>('/api/terminal/takeover', {
    intent: 'terminal-takeover',
    confirmed: true,
  });
}
