import type {
  BriefingResponse,
  DispatchGatesResponse,
  DispatchItem,
  DispatchQueueResponse,
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
  containers_error?: string | null;
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
    notifyApiFailure();
    throw new Error(`${method} ${path} failed: ${resp.status} ${text}`.trim());
  }
  return (await resp.json()) as T;
}

// API failure broadcast (issue #588): the header health pill
// revalidates on any failure so outages stop reading as stale
// all-clear. Returns an unsubscribe function.
type ApiFailureListener = () => void;

const apiFailureListeners = new Set<ApiFailureListener>();

export function subscribeApiFailures(listener: ApiFailureListener): () => void {
  apiFailureListeners.add(listener);
  return () => {
    apiFailureListeners.delete(listener);
  };
}

function notifyApiFailure(): void {
  for (const listener of [...apiFailureListeners]) listener();
}

// Offline mutation outbox (issue #404): operator commands issued
// with dropped connectivity queue locally (persisted) and replay
// on reconnect with a visible sync status. Mutations only — reads
// fail as today. Every mutating POST/DELETE carries an
// Idempotency-Key generated once per attempt and stored with the
// entry, so replays reuse the key instead of minting dupes. Note:
// the server does not enforce keys yet; they make retries safe
// once it does, and document replay identity until then.
export type OutboxMethod = 'POST' | 'DELETE';

export interface OutboxEntry {
  key: string;
  method: OutboxMethod;
  path: string;
  body: unknown;
  queuedAt: number;
}

export interface OutboxStatus {
  online: boolean;
  pending: number;
  syncing: boolean;
  lastError: string | null;
}

export interface FlushResult {
  synced: number;
  failed: number;
  pending: number;
}

// Thrown when a mutation queues instead of sending. Callers
// already surface thrown messages inline, so the queue is
// visible both there and in the SyncBanner.
export class QueuedOfflineError extends Error {
  readonly queued = true;
  readonly entry: OutboxEntry;

  constructor(entry: OutboxEntry, pending: number) {
    super(
      `${entry.method} ${entry.path} queued offline (${pending} pending) — syncs on reconnect`,
    );
    this.name = 'QueuedOfflineError';
    this.entry = entry;
  }
}

const OUTBOX_STORAGE_KEY = 'buttons.outbox.v1';

// In-memory fallback when localStorage is unavailable (SSR,
// tests, private mode). Storage is read through on every
// access so a reload sees the persisted queue.
const outboxMemory: OutboxEntry[] = [];

function isOutboxEntry(value: unknown): value is OutboxEntry {
  if (!value || typeof value !== 'object') return false;
  const e = value as Record<string, unknown>;
  return (
    typeof e.key === 'string'
    && (e.method === 'POST' || e.method === 'DELETE')
    && typeof e.path === 'string'
    && typeof e.queuedAt === 'number'
  );
}

function loadOutbox(): OutboxEntry[] {
  try {
    if (typeof localStorage === 'undefined') return [...outboxMemory];
    const raw = localStorage.getItem(OUTBOX_STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(isOutboxEntry);
  } catch {
    return [...outboxMemory];
  }
}

function saveOutbox(entries: OutboxEntry[]): void {
  try {
    if (typeof localStorage === 'undefined') {
      outboxMemory.splice(0, outboxMemory.length, ...entries);
      return;
    }
    localStorage.setItem(OUTBOX_STORAGE_KEY, JSON.stringify(entries));
  } catch {
    outboxMemory.splice(0, outboxMemory.length, ...entries);
  }
}

export function isOnline(): boolean {
  return typeof navigator === 'undefined' || navigator.onLine !== false;
}

type OutboxListener = () => void;

const outboxListeners = new Set<OutboxListener>();

export function subscribeOutbox(listener: OutboxListener): () => void {
  ensureOnlineListener();
  outboxListeners.add(listener);
  return () => {
    outboxListeners.delete(listener);
  };
}

function notifyOutbox(): void {
  for (const listener of [...outboxListeners]) listener();
}

let outboxSyncing = false;
let outboxLastError: string | null = null;

export function getOutbox(): OutboxEntry[] {
  return loadOutbox();
}

export function getOutboxStatus(): OutboxStatus {
  return {
    online: isOnline(),
    pending: loadOutbox().length,
    syncing: outboxSyncing,
    lastError: outboxLastError,
  };
}

// Drop the whole queue (SyncBanner discard). Replays never
// re-enqueue, so this only clears operator-visible pendings.
export function clearOutbox(): void {
  outboxLastError = null;
  saveOutbox([]);
  notifyOutbox();
}

/** Test-only reset for the outbox (drains queue + sync state). */
export function __clearOutbox(): void {
  outboxSyncing = false;
  clearOutbox();
}

function newIdempotencyKey(): string {
  try {
    const cryptoApi = globalThis.crypto as { randomUUID?: () => string } | undefined;
    if (cryptoApi?.randomUUID) return cryptoApi.randomUUID();
  } catch {
    // Fall through to the Math.random key below.
  }
  return (
    `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
    + `-${Math.random().toString(36).slice(2, 10)}`
  );
}

function queueEntry(method: OutboxMethod, path: string, body: unknown, key: string): never {
  ensureOnlineListener();
  const entries = loadOutbox();
  const entry: OutboxEntry = { key, method, path, body, queuedAt: Date.now() };
  entries.push(entry);
  saveOutbox(entries);
  notifyOutbox();
  throw new QueuedOfflineError(entry, entries.length);
}

function removeOutboxKey(key: string): void {
  saveOutbox(loadOutbox().filter((e) => e.key !== key));
}

// Single choke point for JSON mutations: offline (or a dropped
// connection mid-request) queues + throws QueuedOfflineError;
// HTTP errors surface to the caller and never queue — the
// server answered, so re-sending could double-apply.
async function mutateWith<T>(
  method: OutboxMethod,
  path: string,
  body: unknown,
  read: (resp: Response) => Promise<T>,
  timeoutMs: number = API_TIMEOUT_MS,
): Promise<T> {
  ensureOnlineListener();
  const key = newIdempotencyKey();
  if (!isOnline()) queueEntry(method, path, body, key);
  let resp: Response;
  try {
    resp = await fetchApi(path, {
      method,
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
      body: JSON.stringify(body),
    }, timeoutMs);
  } catch (err) {
    // A timeout is not an offline signal: the server may have
    // applied the mutation, so replaying could double-apply.
    // Surface it instead of queuing.
    if (err instanceof RequestTimeoutError) throw err;
    queueEntry(method, path, body, key);
  }
  return read(resp);
}

// Replay the queue FIFO with each entry's stored key. One run
// at a time; a network failure stops the run and keeps the
// rest, a server rejection keeps that entry and continues with
// the next so one bad payload never blocks the queue.
export async function flushOutbox(): Promise<FlushResult> {
  ensureOnlineListener();
  if (outboxSyncing) return { synced: 0, failed: 0, pending: loadOutbox().length };
  outboxSyncing = true;
  outboxLastError = null;
  notifyOutbox();
  let synced = 0;
  let failed = 0;
  try {
    const snapshot = loadOutbox();
    for (const entry of snapshot) {
      if (!isOnline()) break;
      let resp: Response;
      try {
        resp = await fetchApi(entry.path, {
          method: entry.method,
          headers: { 'Content-Type': 'application/json', 'Idempotency-Key': entry.key },
          body: JSON.stringify(entry.body),
        });
      } catch {
        break;
      }
      if (!resp.ok) {
        const text = await resp.text().catch(() => '');
        failed += 1;
        outboxLastError = `${entry.method} ${entry.path} failed: ${resp.status} ${text}`.trim();
        notifyApiFailure();
        continue;
      }
      removeOutboxKey(entry.key);
      synced += 1;
    }
  } finally {
    outboxSyncing = false;
    notifyOutbox();
  }
  return { synced, failed, pending: loadOutbox().length };
}

let onlineListenerRegistered = false;

function ensureOnlineListener(): void {
  if (onlineListenerRegistered || typeof window === 'undefined') return;
  onlineListenerRegistered = true;
  window.addEventListener('online', () => {
    void flushOutbox();
  });
  window.addEventListener('offline', () => {
    notifyOutbox();
  });
}

// Request timeouts (issue #460): every JSON fetch carries an
// AbortController signal with a bounded budget, so a wedged
// backend fails visibly instead of spinning forever. Generous
// default; known-slow reads (briefing generation, plan reads)
// get a longer budget. Timeout failures throw
// RequestTimeoutError, whose message matches the historyUtils
// /timeout/i friendly-error rule. Streams (postAgentTurn, the
// EventSource pushes) keep user-abort behavior only.
export const API_TIMEOUT_MS = 30_000;
export const API_SLOW_TIMEOUT_MS = 120_000;

export class RequestTimeoutError extends Error {
  readonly path: string;
  readonly budgetMs: number;

  constructor(method: string, path: string, budgetMs: number) {
    super(`${method} ${path} failed: request timeout after ${budgetMs}ms`);
    this.name = 'RequestTimeoutError';
    this.path = path;
    this.budgetMs = budgetMs;
  }
}

async function fetchApi(
  path: string,
  init?: RequestInit,
  timeoutMs: number = API_TIMEOUT_MS,
): Promise<Response> {
  const method = typeof init?.method === 'string' ? init.method : 'GET';
  const ctrl = new AbortController();
  const timer = setTimeout(() => {
    ctrl.abort(new DOMException(`${method} ${path} timed out`, 'TimeoutError'));
  }, timeoutMs);
  try {
    // Every request carries the timeout signal: tests assert its
    // presence, including on plain GETs.
    return await fetch(apiUrl(path), { ...init, signal: ctrl.signal });
  } catch (err) {
    notifyApiFailure();
    if (err instanceof DOMException && err.name === 'TimeoutError') {
      throw new RequestTimeoutError(method, path, timeoutMs);
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

async function getJson<T>(path: string, timeoutMs: number = API_TIMEOUT_MS): Promise<T> {
  return readJson<T>(await fetchApi(path, undefined, timeoutMs), 'GET', path);
}

export function fetchSystemHealth(): Promise<SystemHealthResponse> {
  return getJson<SystemHealthResponse>('api/system/health');
}

// Per-container live CPU/RAM for the header drawer (issue #663).
export interface ContainerStatsRow {
  name: string;
  service: string;
  state: string;
  cpu_pct: number | null;
  mem_used: string | null;
  mem_limit: string | null;
}

export interface ContainerStatsResponse {
  containers: ContainerStatsRow[];
}

export function fetchContainerStats(): Promise<ContainerStatsResponse> {
  return getJson<ContainerStatsResponse>('api/system/stats');
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

// Generation guard (issue #511): invalidateViewCaches bumps
// viewCacheGeneration; a settle writes the cache only when its
// generation is still current, so an in-flight response landing
// after an invalidation cannot re-populate the cache with stale
// data. Settles also drop their in-flight entry only when it is
// still theirs, so a stale settle cannot evict a newer fetch.
let viewCacheGeneration = 0;

function cachedViewFetch<T>(key: string, ttlMs: number, load: () => Promise<T>): Promise<T> {
  const hit = viewCache.get(key);
  if (hit) {
    if (hit.expiresAt > Date.now()) return Promise.resolve(hit.value as T);
    viewCache.delete(key);
  }
  const pending = viewInflight.get(key);
  if (pending) return pending as Promise<T>;
  const generation = viewCacheGeneration;
  const p = load();
  viewInflight.set(key, p);
  p.then(
    (value) => {
      if (viewInflight.get(key) === p) viewInflight.delete(key);
      if (generation !== viewCacheGeneration) return;
      viewCache.set(key, { value, expiresAt: Date.now() + ttlMs });
    },
    () => {
      if (viewInflight.get(key) === p) viewInflight.delete(key);
    },
  );
  return p;
}

/** Drop all cached view reads. Mutations call this; tests reset with it. */
export function invalidateViewCaches(): void {
  viewCacheGeneration += 1;
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
    getJson<BriefingResponse>(path, API_SLOW_TIMEOUT_MS),
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

export async function createIssue(repo: string, title: string, body: string, labels?: string[]): Promise<{ ok: boolean; url: string }> {
  return mutateWith('POST', 'api/create-issue', { repo, title, body, ...(labels?.length ? { labels } : {}) }, async (resp) => {
    if (!resp.ok) {
      notifyApiFailure();
      throw new Error(`POST /api/create-issue failed: ${resp.status}`);
    }
    return (await resp.json()) as { ok: boolean; url: string };
  });
}

export function fetchLedger(
  kind = 'all',
  limit = 50,
  since?: number,
  source?: string,
): Promise<LedgerResponse> {
  const qs = `api/ledger?kind=${encodeURIComponent(kind)}&limit=${limit}`
    + (since !== undefined ? `&since=${since}` : '')
    + (source !== undefined ? `&source=${encodeURIComponent(source)}` : '');
  return getJson<LedgerResponse>(qs);
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

async function postJson<T>(path: string, body: unknown, timeoutMs: number = API_TIMEOUT_MS): Promise<T> {
  return mutateWith<T>('POST', path, body, (resp) => readJson<T>(resp, 'POST', path), timeoutMs);
}

// Server-sent attention action hrefs ('api/…'-relative): the same
// outbox + idempotency treatment as other mutations, so NowView
// taps queue offline instead of bypassing the outbox.
export function postActionHref(href: string, body: unknown): Promise<unknown> {
  return postJson<unknown>(href, body);
}

export function fetchRepos(): Promise<import('./reviewTypes').ReposResponse> {
  return getJson<import('./reviewTypes').ReposResponse>('api/repos');
}

// Status + diff for one checkout, backing the dirty-repo modal (#663).
export interface RepoDiffResponse {
  repo: string;
  branch: string;
  clean: boolean;
  status_short: string;
  diff_stat: string;
  diff: string;
  truncated: boolean;
}

export function fetchRepoDiff(repo: string): Promise<RepoDiffResponse> {
  return getJson<RepoDiffResponse>(`api/repo/diff?repo=${encodeURIComponent(repo)}`);
}

// Worklist metrics for the prefetch strip (issue #897): short TTL,
// local reads, zero model turns. Warmed on repo select so the Chat
// strip renders from cache.
export interface RepoMetricsTodoHit {
  file: string;
  line: number;
  text: string;
}

export interface RepoMetricsResponse {
  repo: string;
  branch: string;
  clean: boolean;
  status_count: number;
  todo_count: number;
  todo_hits: RepoMetricsTodoHit[];
  diff_stat: string;
  diff_files: number;
}

export function fetchRepoMetrics(repo: string): Promise<RepoMetricsResponse> {
  const path = `api/repo/metrics?repo=${encodeURIComponent(repo)}`;
  return cachedViewFetch<RepoMetricsResponse>(path, VIEW_CACHE_SHORT_TTL_MS, () =>
    getJson<RepoMetricsResponse>(path),
  );
}

export function warmRepoMetrics(repo: string): Promise<void> {
  return fetchRepoMetrics(repo).then(
    () => undefined,
    () => undefined,
  );
}

// Repo file paths for @-mention completion in the chat composer
// (#653). Prefix-filtered server-side; truncated means "refine
// the prefix". Paths only, never contents.
export interface RepoFilesResponse {
  repo: string;
  prefix: string;
  files: string[];
  truncated: boolean;
}

export function fetchRepoFiles(repo: string, prefix = ''): Promise<RepoFilesResponse> {
  return getJson<RepoFilesResponse>(
    `api/repo/files?repo=${encodeURIComponent(repo)}&prefix=${encodeURIComponent(prefix)}`,
  );
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

// Explicit action branch (#708): sent only when non-blank, so
// payloads without one stay byte-identical to today.
function withBranch<T extends object>(body: T, branch?: string): T & { branch?: string } {
  const b = branch?.trim();
  return b ? { ...body, branch: b } : body;
}

export function mergePr(
  repo: string,
  prNumber: number,
  branch?: string,
): Promise<{ ok: boolean; output: string }> {
  return postJson<{ ok: boolean; output: string }>(
    'api/pr-action',
    withBranch(
      {
        repo,
        pr_number: prNumber,
        action: 'merge',
        intent: 'pr-merge',
        confirmed: true,
      },
      branch,
    ),
  );
}

export function approvePr(
  repo: string,
  prNumber: number,
  branch?: string,
): Promise<{ ok: boolean; output: string }> {
  return postJson<{ ok: boolean; output: string }>(
    'api/pr-action',
    withBranch(
      {
        repo,
        pr_number: prNumber,
        action: 'approve',
        intent: 'pr-approve',
        confirmed: true,
      },
      branch,
    ),
  );
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
  branch?: string,
): Promise<{ id: string }> {
  return postJson<{ id: string }>(
    'api/jobs/run',
    withBranch(
      confirmed ? { action, repo, intent: 'jobs-run', confirmed: true } : { action, repo },
      branch,
    ),
  ).then((r) => {
    invalidateViewCaches();
    return r;
  });
}

export function startTdRun(
  command: string,
  repo: string,
  branch?: string,
): Promise<{ id: string }> {
  return postJson<{ id: string }>(
    'api/jobs/run-td',
    withBranch({ command, repo }, branch),
  ).then((r) => {
    invalidateViewCaches();
    return r;
  });
}

// Dispatched multi-page UX audit (issue #850): crawl + file one
// findings issue. The run tail ends in AUDIT_DONE <repo>#<n> <url>.
export function startUxAuditRun(
  site: string,
  maxPages: number,
): Promise<{ id: string }> {
  return postJson<{ id: string }>('api/jobs/run-ux-audit', {
    site,
    max_pages: maxPages,
  }).then((r) => {
    invalidateViewCaches();
    return r;
  });
}

export interface UxAuditGroup {
  index: number;
  category: string;
  title: string;
  count: number;
  worst: string;
  body: string;
}

// Finding groups from the latest audit report: one-tap assign
// payloads (each body is a task spec for a focused dispatch).
export function fetchUxAuditGroups(
  site: string,
): Promise<{ site: string; groups: UxAuditGroup[] }> {
  return getJson<{ site: string; groups: UxAuditGroup[] }>(
    `api/jobs/ux-audit/groups?site=${encodeURIComponent(site)}`,
  );
}

// One-click re-run of a finished run through the real launch
// endpoints (same gates, same conflicts as a fresh launch).
// Recorded argv replays to run-td; the join is lossless because
// recorded argv passed validate_td_command, which splits on
// whitespace — except ux-audit argv, which replays to the audit
// endpoint (it is site argv, not a td command). Callers pass
// confirmed=true for destructive actions.
export function rerunJob(
  action: string,
  repo: string,
  tdArgv: string[],
  confirmed = false,
  branch?: string,
): Promise<{ id: string }> {
  if (action === 'ux-audit' && tdArgv[0] === 'ux-audit' && tdArgv[1]) {
    const at = tdArgv.indexOf('--max-pages');
    const pages =
      at >= 0 && tdArgv[at + 1] ? Number.parseInt(tdArgv[at + 1], 10) : 25;
    return startUxAuditRun(
      tdArgv[1],
      Number.isFinite(pages) ? pages : 25,
    );
  }
  if (tdArgv.length > 0) return startTdRun(tdArgv.join(' '), repo, branch);
  return startJobRun(action, repo, confirmed, branch);
}

// Cancel a running run (issue #589): flags the run and SIGTERMs
// its step tree. No confirm — cancel stops spend.
export function cancelJobRun(rid: string): Promise<{ ok: boolean; id: string }> {
  return postJson<{ ok: boolean; id: string }>(
    `api/jobs/runs/${encodeURIComponent(rid)}/cancel`,
    {},
  ).then((r) => {
    invalidateViewCaches();
    return r;
  });
}

export interface JobRunProgress {
  done: boolean;
  output: string;
  exit_code: number | null;
  action: string;
  repo?: string;
  td_argv?: string[];
}

export function fetchJobRun(rid: string): Promise<JobRunProgress> {
  return getJson<JobRunProgress>(`api/jobs/runs/${encodeURIComponent(rid)}`);
}

export interface RunCompareResult {
  a_id: string;
  b_id: string;
  unified_diff: string;
  truncated: boolean;
}

// Unified diff of two runs' full outputs (issue #657).
export function fetchRunCompare(a: string, b: string): Promise<RunCompareResult> {
  return getJson<RunCompareResult>(
    `api/jobs/runs/compare?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`,
  );
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

// Cross-repo open-issue discovery (issue #882): one tagged list
// across every checkout, no picker switching.
export function fetchAllIssues(): Promise<import('./reviewTypes').IssueListResponse> {
  return getJson<import('./reviewTypes').IssueListResponse>('api/issues?repo=all');
}

// Cross-repo lane dispatch queue (issue #853). Reads are plain
// GETs; mutations ride the outbox like other console taps.
export function fetchDispatchQueue(): Promise<DispatchQueueResponse> {
  return getJson<DispatchQueueResponse>('api/dispatch/queue');
}

export function fetchDispatchGates(repo: string, issue: number): Promise<DispatchGatesResponse> {
  return getJson<DispatchGatesResponse>(
    `api/dispatch/gates?repo=${encodeURIComponent(repo)}&issue=${issue}`,
  );
}

export function enqueueDispatch(
  repo: string,
  issue: string,
  title?: string,
): Promise<DispatchItem> {
  return postJson<DispatchItem>('api/dispatch/enqueue', { repo, issue, title: title ?? '' }).then((r) => {
    invalidateViewCaches();
    return r;
  });
}

export function startDispatch(
  repo: string,
  issue: number,
): Promise<{ ok: boolean; repo: string; issue: number; status: string }> {
  return postJson<{ ok: boolean; repo: string; issue: number; status: string }>(
    'api/dispatch/start',
    { repo, issue, intent: 'dispatch-start', confirmed: true },
  ).then((r) => {
    invalidateViewCaches();
    return r;
  });
}

export function cancelDispatch(
  repo: string,
  issue: number,
): Promise<{ ok: boolean; removed: boolean }> {
  return postJson<{ ok: boolean; removed: boolean }>('api/dispatch/cancel', {
    repo,
    issue,
  }).then((r) => {
    invalidateViewCaches();
    return r;
  });
}

async function deleteJson<T>(path: string, body: unknown, timeoutMs: number = API_TIMEOUT_MS): Promise<T> {
  return mutateWith<T>('DELETE', path, body, (resp) => readJson<T>(resp, 'DELETE', path), timeoutMs);
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

export function postChatStream(
  prompt: string,
  repo: string,
  sessionId: string,
  prefer: ChatBackend = 'auto',
  signal?: AbortSignal,
): Promise<Response> {
  // Raw Response: the caller pumps the SSE stream. Prompts ride
  // the POST body, never the URL (#459): query params land in
  // browser history and proxy access logs. No request timeout:
  // streams keep user-abort behavior only, like postAgentTurn.
  return fetch(apiUrl('api/chat/stream'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prompt, repo, session_id: sessionId, prefer }),
    signal,
  });
}

export function abortChat(sessionId: string): Promise<{ ok: boolean; session_id: string }> {
  return postJson<{ ok: boolean; session_id: string }>('api/chat/abort', {
    session_id: sessionId,
  });
}

export function requestTakeover(): Promise<{ ok: boolean; matched: boolean; output: string }> {
  return postJson<{ ok: boolean; matched: boolean; output: string }>('api/terminal/takeover', {
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
  repo?: string | null;
  schedules?: RepairSchedule[];
}

export function dispatchRepair(
  issue: string,
  branch?: string,
  repo?: string,
  focus?: string,
): Promise<{ ok: boolean; issue: number; status: string; repo: string }> {
  return postJson<{ ok: boolean; issue: number; status: string; repo: string }>(
    'api/repair/dispatch',
    withBranch(
      {
        issue,
        ...(repo?.trim() ? { repo: repo.trim() } : {}),
        ...(focus ? { focus } : {}),
        intent: 'repair-dispatch',
        confirmed: true,
      },
      branch,
    ),
  );
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

export function fetchVerifyCommand(issue: string, repo?: string): Promise<RepairVerifyPreview> {
  const query = `issue=${encodeURIComponent(issue)}${repo?.trim() ? `&repo=${encodeURIComponent(repo.trim())}` : ''}`;
  return getJson<RepairVerifyPreview>(
    `api/repair/verify-command?${query}`,
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
    API_SLOW_TIMEOUT_MS,
  );
}

export interface AgentRun {
  id: string;
  script: string;
  model: string;
  provider: string | null;
  thread: string;
  started_ts: number;
  duration_s: number | null;
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
  reclaim?: boolean,
): Promise<Response> {
  // Raw Response: the caller pumps the SSE stream. A new conversation
  // (no session id) carries the hold-to-confirm intent; follow-ups
  // reuse the session the done event returned. A reclaimed orphan
  // (issue #856) keeps its id but confirms like a new spawn, since
  // the server no longer knows it. Streaming turns bypass the
  // offline outbox: a replayed stream has no consumer, so they fail
  // as today when offline.
  const fresh = !sessionId || reclaim === true;
  return fetch(apiUrl('api/agent/turn'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      provider,
      repo,
      prompt,
      session_id: sessionId ?? '',
      intent: fresh ? 'agent-spawn' : '',
      confirmed: fresh,
    }),
    signal,
  });
}

// Reattach state for one agent session (issue #856): a reloaded
// console polls this to resume a live turn's text or learn the
// verdict without firing a duplicate turn.
export interface AgentSessionFinished {
  exit: number | null;
  stderr: string;
  truncated: boolean;
  timed_out: boolean;
  timeout: number;
  rescue: {
    branch: string | null;
    commit: string | null;
    committed: boolean;
    pushed: boolean;
  } | null;
  guidance_code?: string | null;
  guidance_message?: string;
  guidance_detail?: string;
}

export interface AgentSessionState {
  session_id: string;
  known: boolean;
  running: boolean;
  transcript: string;
  transcript_truncated: boolean;
  finished: AgentSessionFinished | null;
}

export function fetchAgentSession(
  sessionId: string,
): Promise<AgentSessionState> {
  return getJson<AgentSessionState>(
    `api/agent/session?session_id=${encodeURIComponent(sessionId)}`,
  );
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

// Notification preferences (issue #656). One object, one round
// trip: muted event classes plus the toggleable vocabulary.
export interface NotificationPreferences {
  muted_events: string[];
  events: string[];
}

export function fetchNotificationPreferences(): Promise<NotificationPreferences> {
  return getJson<NotificationPreferences>('api/notifications/preferences');
}

export function saveNotificationPreferences(body: {
  muted_events?: string[];
}): Promise<NotificationPreferences> {
  return postJson<NotificationPreferences>('api/notifications/preferences', body);
}

// Scheduled digest cadence (issue #636): daily/weekly/off plus
// next-run and last-send bookkeeping from the tick-fired job.
export interface DigestSchedule {
  cadence: string;
  next_run: number | null;
  last_sent: number | null;
  last_result: { ok?: boolean; at?: number; error?: string };
}

export function fetchDigestSchedule(): Promise<DigestSchedule> {
  return getJson<DigestSchedule>('api/notifications/digest-schedule');
}

export function setDigestSchedule(cadence: string): Promise<DigestSchedule> {
  return postJson<DigestSchedule>('api/notifications/digest-schedule', { cadence });
}

// Scheduled jobs actions (issue #655): any non-destructive action
// (plus run-td argv) can carry a daily/weekly cadence; due runs
// fire through the standard launch path on the shared tick.
export interface ActionSchedule {
  id: string;
  action: string;
  td_argv: string[];
  repo: string;
  cadence: string;
  next_run: number;
  last_run: number | null;
  last_result: { ok?: boolean; at?: number; run_id?: string; error?: string };
  enabled: boolean;
}

export interface ActionScheduleList {
  schedules: ActionSchedule[];
}

export function fetchActionSchedules(): Promise<ActionScheduleList> {
  return getJson<ActionScheduleList>('api/jobs/schedule');
}

export function createActionSchedule(body: {
  action: string;
  td_argv: string[];
  repo: string;
  cadence: string;
}): Promise<{ ok: boolean; schedule: ActionSchedule }> {
  return postJson<{ ok: boolean; schedule: ActionSchedule }>('api/jobs/schedule', body).then(
    (r) => {
      invalidateViewCaches();
      return r;
    },
  );
}

export function deleteActionSchedule(id: string): Promise<{ ok: boolean }> {
  return deleteJson<{ ok: boolean }>(`api/jobs/schedule/${encodeURIComponent(id)}`, {}).then(
    (r) => {
      invalidateViewCaches();
      return r;
    },
  );
}

export function pauseActionSchedule(id: string): Promise<{ ok: boolean; schedule: ActionSchedule }> {
  return postJson<{ ok: boolean; schedule: ActionSchedule }>(
    `api/jobs/schedule/${encodeURIComponent(id)}/pause`,
    {},
  ).then((r) => {
    invalidateViewCaches();
    return r;
  });
}

export function resumeActionSchedule(id: string): Promise<{ ok: boolean; schedule: ActionSchedule }> {
  return postJson<{ ok: boolean; schedule: ActionSchedule }>(
    `api/jobs/schedule/${encodeURIComponent(id)}/resume`,
    {},
  ).then((r) => {
    invalidateViewCaches();
    return r;
  });
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
