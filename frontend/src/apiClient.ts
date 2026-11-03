import type {
  BriefingResponse,
  JobsResponse,
  LedgerResponse,
} from './apiTypes';

export interface DiskSummary {
  summary?: string;
  percent?: number;
  error?: string | null;
}

export interface ContainerStatus {
  name?: string;
  service?: string;
  state?: string;
  status?: string;
  health?: string;
}

export interface SystemHealthResponse {
  disk: DiskSummary;
  memory: DiskSummary;
  containers: ContainerStatus[];
}

// Absolute API URLs from Vite's base: '/' in dev, '/buttons/' in the
// image build. Relative 'api/…' breaks on slashless page URLs (the
// browser resolves them to /), and root-absolute '/api/…' bypasses
// the /buttons prefix Caddy strips. Server-sent hrefs stay
// 'api/…'-relative; apiUrl() absolutizes both shapes at the edge.
const API_BASE: string = import.meta.env.BASE_URL;

export function apiUrl(path: string): string {
  if (/^(https?:)?\/\//.test(path) || path.startsWith('/')) return path;
  return `${API_BASE}${path}`;
}

async function readJson<T>(resp: Response, method: string, path: string): Promise<T> {
  if (!resp.ok) {
    const text = await resp.text().catch(() => '');
    throw new Error(`${method} ${path} failed: ${resp.status} ${text}`.trim());
  }
  return (await resp.json()) as T;
}

async function getJson<T>(path: string): Promise<T> {
  return readJson<T>(await fetch(apiUrl(path)), 'GET', path);
}

export function fetchSystemHealth(): Promise<SystemHealthResponse> {
  return getJson<SystemHealthResponse>('api/system/health');
}

export function fetchJobs(): Promise<JobsResponse> {
  return getJson<JobsResponse>('api/jobs');
}

export function fetchBriefing(repo: string): Promise<BriefingResponse> {
  return getJson<BriefingResponse>(
    `api/briefing?repo=${encodeURIComponent(repo)}`,
  );
}

export function fetchAttention(repo: string): Promise<import('./attentionTypes').AttentionResponse> {
  return getJson<import('./attentionTypes').AttentionResponse>(
    `api/attention?repo=${encodeURIComponent(repo)}`,
  );
}

export async function createIssue(repo: string, title: string, body: string): Promise<{ ok: boolean; url: string }> {
  const resp = await fetch(apiUrl('api/create-issue'), {
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
    `api/ledger?kind=${encodeURIComponent(kind)}&limit=${limit}`,
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
    `api/workflow-health?repo=${encodeURIComponent(repo)}`,
  );
}

export interface PiProgressResponse {
  done: boolean;
  output: string;
  result?: Record<string, unknown> | null;
  error?: string | null;
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const resp = await fetch(apiUrl(path), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return readJson<T>(resp, 'POST', path);
}

export function fetchRepos(): Promise<import('./reviewTypes').ReposResponse> {
  return getJson<import('./reviewTypes').ReposResponse>('api/repos');
}

export function fetchPrs(repo: string): Promise<import('./reviewTypes').PrListResponse> {
  return getJson<import('./reviewTypes').PrListResponse>(
    `api/prs?repo=${encodeURIComponent(repo)}`,
  );
}

export interface PrDiffResponse {
  number: number;
  diff: string;
  truncated: boolean;
}

export function fetchPrDiff(repo: string, pr: number): Promise<PrDiffResponse> {
  return getJson<PrDiffResponse>(
    `api/pr-diff?repo=${encodeURIComponent(repo)}&pr=${pr}`,
  );
}

export function fetchPrDetails(repo: string, pr: number): Promise<import('./reviewTypes').PrDetailsResponse> {
  return getJson<import('./reviewTypes').PrDetailsResponse>(
    `api/pr-details?repo=${encodeURIComponent(repo)}&pr=${pr}`,
  );
}

export function mergePr(repo: string, prNumber: number): Promise<{ ok: boolean; output: string }> {
  return postJson<{ ok: boolean; output: string }>('api/pr-action', {
    repo,
    pr_number: prNumber,
    action: 'merge',
    intent: 'pr-merge',
    confirmed: true,
  });
}

export function startJobRun(
  action: string,
  repo: string,
  confirmed = false,
): Promise<{ id: string }> {
  return postJson<{ id: string }>(
     'api/jobs/run',
    confirmed ? { action, repo, intent: 'jobs-run', confirmed: true } : { action, repo },
  );
}

export function startTdRun(
  command: string,
  repo: string,
): Promise<{ id: string }> {
  return postJson<{ id: string }>('api/jobs/run-td', { command, repo });
}

export interface JobRunProgress {
  done: boolean;
  output: string;
  exit_code: number | null;
  action: string;
}

export function fetchJobRun(rid: string): Promise<JobRunProgress> {
  return getJson<JobRunProgress>(`api/jobs/runs/${encodeURIComponent(rid)}`);
}

export function clearJobs(all: boolean): Promise<{ ok: boolean; cleared: number }> {
  return postJson<{ ok: boolean; cleared: number }>('api/jobs/clear', {
    all,
    intent: 'jobs-clear',
    confirmed: true,
  });
}

export function fetchIssues(repo: string): Promise<import('./reviewTypes').IssueListResponse> {
  return getJson<import('./reviewTypes').IssueListResponse>(
    `api/issues?repo=${encodeURIComponent(repo)}`,
  );
}

export function draftIssue(repo: string, title: string, cause: string, details: string): Promise<import('./reviewTypes').IssueDraftResponse> {
  return postJson<import('./reviewTypes').IssueDraftResponse>('api/issue-draft', {
    repo,
    source: 'finding',
    title,
    cause,
    details,
  });
}

export function fetchPiProgress(rid: string): Promise<PiProgressResponse> {
  return getJson<PiProgressResponse>(`api/pi-progress/${encodeURIComponent(rid)}`);
}

export function chatStreamUrl(prompt: string, repo: string, sessionId: string): string {
  const q = new URLSearchParams({ prompt, repo, session_id: sessionId });
  return apiUrl(`api/chat/stream?${q.toString()}`);
}

export function abortChat(sessionId: string): Promise<{ ok: boolean; session_id: string }> {
  return postJson<{ ok: boolean; session_id: string }>('api/chat/abort', {
    session_id: sessionId,
  });
}

export function requestTakeover(): Promise<{ ok: boolean; output: string }> {
  return postJson<{ ok: boolean; output: string }>('api/terminal/takeover', {
    intent: 'terminal-takeover',
    confirmed: true,
  });
}

export interface RepairStatusResponse {
  status: string;
  timestamp: number | null;
  log_buffer: string;
  issue: number | null;
  exit_code: number | null;
}

export function dispatchRepair(issue: string): Promise<{ ok: boolean; issue: number; status: string }> {
  return postJson<{ ok: boolean; issue: number; status: string }>('api/repair/dispatch', {
    issue,
  });
}

export function fetchRepairStatus(): Promise<RepairStatusResponse> {
  return getJson<RepairStatusResponse>('api/repair/status');
}

export interface PlanReadResponse {
  repo: string;
  name: string;
  content: string;
}

export function fetchPlan(repo: string, name: string): Promise<PlanReadResponse> {
  return getJson<PlanReadResponse>(
    `api/jobs/plans/${encodeURIComponent(name)}?repo=${encodeURIComponent(repo)}`,
  );
}

export interface AgentRun {
  id: string;
  script: string;
  model: string;
  thread: string;
  started_ts: number;
  duration_s: number;
  ok: boolean;
  tools: string[];
  tokens: number;
  preview: string;
}

export interface TraceEvent {
  ts: number;
  type: string;
  [key: string]: unknown;
}

export interface AgentRunDetail {
  id: string;
  entry: Record<string, unknown>;
  trace: TraceEvent[];
}

export function fetchAgentRuns(limit = 50): Promise<{ runs: AgentRun[] }> {
  return getJson<{ runs: AgentRun[] }>(`api/agents/runs?limit=${limit}`);
}

export function fetchAgentRun(runId: string): Promise<AgentRunDetail> {
  return getJson<AgentRunDetail>(`api/agents/runs/${encodeURIComponent(runId)}`);
}

export interface ApprovalPayload {
  session_id: string;
  action_requests?: Array<{ args?: Record<string, unknown> } & Record<string, unknown>>;
  proposals?: string[];
  [key: string]: unknown;
}

export interface ResumeResponse {
  ok: boolean;
  resumed: boolean;
  session_id: string;
  text: string;
  exit: number;
  approval?: ApprovalPayload;
}

export function resumeChat(
  sessionId: string,
  decision: 'approve' | 'reject',
  message = '',
): Promise<ResumeResponse> {
  return postJson<ResumeResponse>('api/chat/resume', {
    session_id: sessionId,
    decision,
    message,
  });
}
