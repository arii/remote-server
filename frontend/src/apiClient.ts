import type {
  BriefingResponse,
  JobRecord,
  JobsResponse,
  LedgerResponse,
} from './apiTypes';

interface DiskSummary {
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

// Short-lived in-memory cache + in-flight dedup for slow view reads
// (briefing generation, jobs, attention). Views unmount on tab switch;
// the module-level cache survives so revisits land instantly. Keyed by
// request path; successes cached briefly, failures never cached,
// mutations invalidate.
const VIEW_CACHE_TTL_MS = 60_000;
const VIEW_CACHE_SHORT_TTL_MS = 30_000;

interface ViewCacheEntry {
  value: unknown;
  expiresAt: number;
}

const viewCache = new Map<string, ViewCacheEntry>();
const viewInflight = new Map<string, Promise<unknown>>();

function cachedViewFetch<T>(key: string, ttlMs: number, load: () => Promise<T>): Promise<T> {
  const hit = viewCache.get(key);
  if (hit) {
    if (hit.expiresAt > Date.now()) return Promise.resolve(hit.value as T);
    viewCache.delete(key);
  }
  const pending = viewInflight.get(key);
  if (pending) return pending as Promise<T>;
  const p = load();
  viewInflight.set(key, p);
  p.then(
    (value) => {
      viewCache.set(key, { value, expiresAt: Date.now() + ttlMs });
      viewInflight.delete(key);
    },
    () => {
      viewInflight.delete(key);
    },
  );
  return p;
}

/** Drop all cached view reads. Mutations call this; tests reset with it. */
export function invalidateViewCaches(): void {
  viewCache.clear();
  viewInflight.clear();
}

/** Fire-and-forget warm of slow view reads (called on idle from Now). */
export function warmViewCaches(repo: string): Promise<void> {
  return Promise.all([
    fetchBriefing(repo).catch(() => undefined),
    fetchJobs().catch(() => undefined),
    fetchAttention(repo).catch(() => undefined),
  ]).then(() => undefined);
}

export function fetchJobs(): Promise<JobsResponse> {
  return cachedViewFetch<JobsResponse>('api/jobs', VIEW_CACHE_SHORT_TTL_MS, () =>
    getJson<JobsResponse>('api/jobs'),
  );
}

export function fetchBriefing(repo: string): Promise<BriefingResponse> {
  const path = `api/briefing?repo=${encodeURIComponent(repo)}`;
  return cachedViewFetch<BriefingResponse>(path, VIEW_CACHE_TTL_MS, () =>
    getJson<BriefingResponse>(path),
  );
}

export function fetchAttention(repo: string): Promise<import('./attentionTypes').AttentionResponse> {
  const path = `api/attention?repo=${encodeURIComponent(repo)}`;
  return cachedViewFetch<import('./attentionTypes').AttentionResponse>(path, VIEW_CACHE_SHORT_TTL_MS, () =>
    getJson<import('./attentionTypes').AttentionResponse>(path),
  );
}

export interface InboxFeed {
  repo: string;
  updated_at: number;
  attention: import('./attentionTypes').AttentionItem[];
  merge_ready: {
    count: number;
    top: Array<{ number: number; title: string }>;
  };
  runs: {
    active: { id: string; action: string; started: number | null } | null;
    recent: Array<{ id: string; action: string; exit: number | null }>;
  };
}

export function fetchInbox(repo: string): Promise<InboxFeed> {
  return getJson<InboxFeed>(`api/inbox?repo=${encodeURIComponent(repo)}`);
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

interface HiddenStructural {
  count: number;
  files: string[];
}

export interface PrDiffResponse {
  number: number;
  diff: string;
  truncated: boolean;
  hidden_structural?: HiddenStructural;
}

// Short-lived in-memory cache + in-flight dedup for PR details/diff,
// backing silent next-item prefetch in Review triage. Keyed by
// repo+number; successes cached briefly, failures never cached.
const PR_CACHE_TTL_MS = 60_000;

interface PrCacheEntry<T> {
  value: T;
  expiresAt: number;
}

const prDetailsCache = new Map<string, PrCacheEntry<import('./reviewTypes').PrDetailsResponse>>();
const prDiffCache = new Map<string, PrCacheEntry<PrDiffResponse>>();
const prDetailsInflight = new Map<string, Promise<import('./reviewTypes').PrDetailsResponse>>();
const prDiffInflight = new Map<string, Promise<PrDiffResponse>>();

function prCacheKey(repo: string, pr: number): string {
  return `${repo}#${pr}`;
}

function cachedPrFetch<T>(
  cache: Map<string, PrCacheEntry<T>>,
  inflight: Map<string, Promise<T>>,
  key: string,
  load: () => Promise<T>,
): Promise<T> {
  const hit = cache.get(key);
  if (hit) {
    if (hit.expiresAt > Date.now()) return Promise.resolve(hit.value);
    cache.delete(key);
  }
  const pending = inflight.get(key);
  if (pending) return pending;
  const p = load();
  inflight.set(key, p);
  p.then(
    (value) => {
      cache.set(key, { value, expiresAt: Date.now() + PR_CACHE_TTL_MS });
      inflight.delete(key);
    },
    () => {
      inflight.delete(key);
    },
  );
  return p;
}

/** Test-only reset for the PR details/diff caches. */
export function __clearPrCaches(): void {
  prDetailsCache.clear();
  prDiffCache.clear();
  prDetailsInflight.clear();
  prDiffInflight.clear();
}

export function fetchPrDiff(repo: string, pr: number): Promise<PrDiffResponse> {
  return cachedPrFetch(prDiffCache, prDiffInflight, prCacheKey(repo, pr), () =>
    getJson<PrDiffResponse>(
      `api/pr-diff?repo=${encodeURIComponent(repo)}&pr=${pr}`,
    ),
  );
}

export function fetchPrDetails(repo: string, pr: number): Promise<import('./reviewTypes').PrDetailsResponse> {
  return cachedPrFetch(prDetailsCache, prDetailsInflight, prCacheKey(repo, pr), () =>
    getJson<import('./reviewTypes').PrDetailsResponse>(
      `api/pr-details?repo=${encodeURIComponent(repo)}&pr=${pr}`,
    ),
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

export function approvePr(repo: string, prNumber: number): Promise<{ ok: boolean; output: string }> {
  return postJson<{ ok: boolean; output: string }>('api/pr-action', {
    repo,
    pr_number: prNumber,
    action: 'approve',
    intent: 'pr-approve',
    confirmed: true,
  });
}

export function requestPrChanges(
  repo: string,
  prNumber: number,
  body: string,
): Promise<{ ok: boolean; output: string }> {
  return postJson<{ ok: boolean; output: string }>('api/pr-action', {
    repo,
    pr_number: prNumber,
    action: 'request-changes',
    body,
    intent: 'pr-request-changes',
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
  ).then((r) => {
    invalidateViewCaches();
    return r;
  });
}

export function startTdRun(
  command: string,
  repo: string,
): Promise<{ id: string }> {
  return postJson<{ id: string }>('api/jobs/run-td', { command, repo }).then((r) => {
    invalidateViewCaches();
    return r;
  });
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

export function jobsStreamUrl(): string {
  return apiUrl('api/jobs/stream');
}

export function jobRunStreamUrl(rid: string): string {
  return apiUrl(`api/jobs/runs/${encodeURIComponent(rid)}/stream`);
}

// Minimal EventSource surface the subscribers need; injectable so
// tests drive the state machine with a fake.
export interface EventSourceLike {
  addEventListener(type: string, listener: (e: { data: string }) => void): void;
  close(): void;
  onmessage: ((e: { data: string }) => void) | null;
  onerror: (() => void) | null;
}

export type EventSourceFactory = (url: string) => EventSourceLike;

function defaultEventSource(url: string): EventSourceLike {
  return new EventSource(url) as unknown as EventSourceLike;
}

function parseFrameJobs(data: string): JobRecord[] | null {
  try {
    const body = JSON.parse(data) as { jobs?: unknown };
    return Array.isArray(body.jobs) ? (body.jobs as JobRecord[]) : null;
  } catch {
    return null;
  }
}

function parseFrameProgress(data: string): JobRunProgress | null {
  try {
    const body = JSON.parse(data) as Partial<JobRunProgress>;
    if (typeof body.done !== 'boolean' || typeof body.output !== 'string') return null;
    return body as JobRunProgress;
  } catch {
    return null;
  }
}

// Job-list push subscription. Reports liveness so the caller can
// run its polling loop only while the channel is down; closes on
// error (no retry storm — polling covers the gap, push resumes on
// next mount). Returns an unsubscribe cleanup. No-ops when
// EventSource is unavailable (SSR/tests): caller polls instead.
export function subscribeJobsPush(
  handler: { onJobs: (jobs: JobRecord[]) => void; onLive: (live: boolean) => void },
  createSource: EventSourceFactory | null = null,
): () => void {
  const create = createSource ?? defaultEventSource;
  if (createSource === null && typeof EventSource === 'undefined') {
    handler.onLive(false);
    return () => {};
  }
  const src = create(jobsStreamUrl());
  const onJobs = (e: { data: string }) => {
    const jobs = parseFrameJobs(e.data);
    if (jobs === null) return;
    handler.onLive(true);
    handler.onJobs(jobs);
  };
  // The server names list frames `jobs`; accept unnamed too.
  src.addEventListener('jobs', onJobs);
  src.onmessage = onJobs;
  src.onerror = () => {
    handler.onLive(false);
    src.close();
  };
  return () => src.close();
}

// Per-run push subscription. onProgress per frame, onDone on the
// done frame (stream ends server-side), onDead when the channel
// dies or the run vanishes before done (caller falls back to a
// snapshot fetch, then polling).
export function subscribeRunPush(
  rid: string,
  handler: {
    onProgress: (p: JobRunProgress) => void;
    onDone: (p: JobRunProgress) => void;
    onDead: () => void;
  },
  createSource: EventSourceFactory | null = null,
): () => void {
  const create = createSource ?? defaultEventSource;
  if (createSource === null && typeof EventSource === 'undefined') {
    handler.onDead();
    return () => {};
  }
  let done = false;
  const src = create(jobRunStreamUrl(rid));
  const onProgress = (e: { data: string }) => {
    const p = parseFrameProgress(e.data);
    if (p === null) return;
    if (p.done) {
      done = true;
      src.close();
      handler.onDone(p);
      return;
    }
    handler.onProgress(p);
  };
  const onGoneFrame = () => {
    // Server-side gone frame (run cleared): dead either way.
    done = true;
    src.close();
    handler.onDead();
  };
  src.addEventListener('progress', onProgress);
  src.addEventListener('gone', onGoneFrame);
  src.onmessage = onProgress;
  src.onerror = () => {
    src.close();
    if (!done) handler.onDead();
  };
  return () => src.close();
}

export function clearJobs(all: boolean): Promise<{ ok: boolean; cleared: number }> {
  return postJson<{ ok: boolean; cleared: number }>('api/jobs/clear', {
    all,
    intent: 'jobs-clear',
    confirmed: true,
  }).then((r) => {
    invalidateViewCaches();
    return r;
  });
}

export function fetchIssues(repo: string): Promise<import('./reviewTypes').IssueListResponse> {
  return getJson<import('./reviewTypes').IssueListResponse>(
    `api/issues?repo=${encodeURIComponent(repo)}`,
  );
}

async function deleteJson<T>(path: string, body: unknown): Promise<T> {
  const resp = await fetch(apiUrl(path), {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return readJson<T>(resp, 'DELETE', path);
}

export function fetchDeferred(repo: string): Promise<import('./reviewTypes').DeferredListResponse> {
  return getJson<import('./reviewTypes').DeferredListResponse>(
    `api/deferred?repo=${encodeURIComponent(repo)}`,
  );
}

export function deferRow(
  repo: string,
  kind: 'pr' | 'issue',
  number: number,
): Promise<import('./reviewTypes').DeferredListResponse> {
  return postJson<import('./reviewTypes').DeferredListResponse>('api/deferred', {
    repo,
    kind,
    number,
  });
}

export function restoreRow(
  repo: string,
  kind: 'pr' | 'issue',
  number: number,
): Promise<import('./reviewTypes').DeferredListResponse> {
  return deleteJson<import('./reviewTypes').DeferredListResponse>('api/deferred', {
    repo,
    kind,
    number,
  });
}

export function deferRowsBulk(
  repo: string,
  op: 'defer' | 'restore',
  rows: Array<{ kind: 'pr' | 'issue'; number: number }>,
): Promise<import('./reviewTypes').DeferredListResponse> {
  return postJson<import('./reviewTypes').DeferredListResponse>('api/deferred/bulk', {
    repo,
    op,
    rows,
  });
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

export interface RepoLabelsResponse {
  repo: string;
  labels: string[];
}

export function fetchRepoLabels(repo: string): Promise<RepoLabelsResponse> {
  return getJson<RepoLabelsResponse>(
    `api/labels?repo=${encodeURIComponent(repo)}`,
  );
}

export function editIssueLabel(
  repo: string,
  issue: number,
  label: string,
  op: 'add' | 'remove',
): Promise<{ ok: boolean; issue: number }> {
  return postJson<{ ok: boolean; issue: number }>('api/issue-label', {
    repo,
    issue,
    label,
    op,
  });
}

export function fetchPiProgress(rid: string): Promise<PiProgressResponse> {
  return getJson<PiProgressResponse>(`api/pi-progress/${encodeURIComponent(rid)}`);
}

export type ChatBackend = 'auto' | 'local' | 'cloud';

export interface RouteHintResponse {
  ok: boolean;
  provider: string;
  model: string;
  metered: boolean;
  rate_in_per_1m: number | null;
  rate_out_per_1m: number | null;
  error: string;
}

export function fetchRouteHint(prefer: ChatBackend): Promise<RouteHintResponse> {
  return getJson<RouteHintResponse>(`api/chat/route-hint?prefer=${encodeURIComponent(prefer)}`);
}

export function chatStreamUrl(
  prompt: string,
  repo: string,
  sessionId: string,
  prefer: ChatBackend = 'auto',
): string {
  const q = new URLSearchParams({ prompt, repo, session_id: sessionId, prefer });
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

export interface RepairSchedule {
  issue: number;
  cadence: string;
  next_run: number;
  last_run: number | null;
}

export interface RepairStatusResponse {
  status: string;
  timestamp: number | null;
  log_buffer: string;
  issue: number | null;
  exit_code: number | null;
  schedules?: RepairSchedule[];
}

export function dispatchRepair(issue: string): Promise<{ ok: boolean; issue: number; status: string }> {
  return postJson<{ ok: boolean; issue: number; status: string }>('api/repair/dispatch', {
    issue,
  });
}

export function fetchRepairStatus(): Promise<RepairStatusResponse> {
  return getJson<RepairStatusResponse>('api/repair/status');
}

export function abortRepair(): Promise<{ ok: boolean; killed: boolean }> {
  return postJson<{ ok: boolean; killed: boolean }>('api/repair/abort', {});
}

export function setRepairSchedule(
  issue: string,
  cadence: string,
): Promise<{ ok: boolean; schedule: RepairSchedule | null }> {
  return postJson<{ ok: boolean; schedule: RepairSchedule | null }>('api/repair/schedule', {
    issue,
    cadence,
  });
}

export interface RepairVerifyPreview {
  ok: boolean;
  issue: number;
  command: string;
  allowlisted: boolean;
  error: string;
}

export function fetchVerifyCommand(issue: string): Promise<RepairVerifyPreview> {
  return getJson<RepairVerifyPreview>(
    `api/repair/verify-command?issue=${encodeURIComponent(issue)}`,
  );
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
  has_prompt: boolean;
  cost_usd: number | null;
  metered: boolean;
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

export function deleteAgentRun(runId: string): Promise<{ ok: boolean; id: string }> {
  return deleteJson<{ ok: boolean; id: string }>(
    `api/agents/runs/${encodeURIComponent(runId)}`,
    { intent: 'agents-delete', confirmed: true },
  ).then((r) => {
    invalidateViewCaches();
    return r;
  });
}

export function clearAgentRuns(): Promise<{ ok: boolean; cleared: number }> {
  return postJson<{ ok: boolean; cleared: number }>('api/agents/runs/clear', {
    intent: 'agents-clear',
    confirmed: true,
  }).then((r) => {
    invalidateViewCaches();
    return r;
  });
}

export interface BudgetStatus {
  cap_usd: number | null;
  spent_usd: number;
  remaining_usd: number | null;
  blocked: boolean;
  unknown: number;
}

export function fetchBudget(): Promise<BudgetStatus> {
  return getJson<BudgetStatus>('api/agents/budget');
}

export interface AgentProviderStatus {
  name: string;
  available: boolean;
  checked_at: number;
  detail: string;
}

export function fetchAgentProviders(): Promise<{
  providers: string[];
  status?: AgentProviderStatus[];
}> {
  return getJson<{ providers: string[]; status?: AgentProviderStatus[] }>(
    'api/agent/providers',
  );
}

export function postAgentTurn(
  provider: string,
  repo: string,
  prompt: string,
  sessionId: string | null,
  signal?: AbortSignal,
): Promise<Response> {
  // Raw Response: the caller pumps the SSE stream. A new conversation
  // (no session id) carries the hold-to-confirm intent; follow-ups
  // reuse the session the done event returned.
  return fetch(apiUrl('api/agent/turn'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      provider,
      repo,
      prompt,
      session_id: sessionId ?? '',
      intent: sessionId ? '' : 'agent-spawn',
      confirmed: !sessionId,
    }),
    signal,
  });
}

export function abortAgentSession(
  sessionId: string,
): Promise<{ ok: boolean; killed: boolean }> {
  return postJson<{ ok: boolean; killed: boolean }>('api/agent/abort', {
    session_id: sessionId,
  });
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

// Manual digest trigger + status (issue #615). Status stays
// uncached: the controls re-read it after every run so the
// last-digest line never shows a stale stamp.
export interface NotificationsStatus {
  ntfy_configured: boolean;
  topic: string;
  webhook_configured: boolean;
  digest_interval_hours: number;
  last_digest: number;
  enabled_events: string[];
}

export interface DigestChannelResult {
  ok: boolean;
  status?: number;
  error?: string;
}

export interface DigestResult {
  ok: boolean;
  results: Record<string, DigestChannelResult>;
  digest: string;
}

export function fetchNotificationStatus(): Promise<NotificationsStatus> {
  return getJson<NotificationsStatus>('api/notifications/status');
}

export function triggerDigest(): Promise<DigestResult> {
  return postJson<DigestResult>('api/notifications/trigger-digest', {});
}

// Attention snooze (issue #614): hide a queue item id until expiry.
export interface SnoozeResult {
  ok: boolean;
  id: string;
  snoozed_until: number;
}

export interface UnsnoozeResult {
  ok: boolean;
  id: string;
  snoozed: boolean;
}

export function snoozeAttention(
  id: string,
  minutes: number,
  repo = 'remote-server',
): Promise<SnoozeResult> {
  return postJson<SnoozeResult>('api/attention/snooze', { id, minutes, repo });
}

export function unsnoozeAttention(id: string): Promise<UnsnoozeResult> {
  return postJson<UnsnoozeResult>('api/attention/unsnooze', { id });
}
