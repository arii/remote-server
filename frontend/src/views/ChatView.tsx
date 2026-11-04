import { Fragment, useEffect, useRef, useState } from 'react';
import HoldToConfirm from '../components/HoldToConfirm';
import TapToFire from '../components/TapToFire';
import {
  abortChat,
  chatStreamUrl,
  clearAgentRuns,
  deleteAgentRun,
  fetchAgentRun,
  fetchAgentRuns,
  fetchBudget,
  fetchRouteHint,
  resumeChat,
} from '../apiClient';
import type {
  AgentRun,
  AgentRunDetail,
  ApprovalPayload,
  BudgetStatus,
  ChatBackend,
  RouteHintResponse,
  TraceEvent,
} from '../apiClient';
import { loadChat, useMock, type ChatMessage } from './mockViews';
import AgentDrawer from './AgentDrawer';
import RepoLabel from '../components/RepoLabel';

// Standard chat send: Ctrl+Enter / Cmd+Enter sends from the
// prompt box; plain Enter keeps its newline.
export function isKeyboardSend(e: {
  key: string;
  ctrlKey: boolean;
  metaKey: boolean;
}): boolean {
  return e.key === 'Enter' && (e.ctrlKey || e.metaKey);
}

// Composer backend picker options: Auto keeps today's ladder,
// Local only forces Ollama (fails instead of spending), Cloud
// allowed forces Nebius for the turn.
export const CHAT_BACKEND_OPTIONS: Array<{ value: ChatBackend; label: string }> = [
  { value: 'auto', label: 'Auto' },
  { value: 'local', label: 'Local only' },
  { value: 'cloud', label: 'Cloud allowed' },
];

// Streamed tokens carry raw agent stdout, which ends with the
// INTERRUPT blob on approval turns. The backend strips markers
// from done.text; strip them here too so the chat log stays human.
export function cleanAgentText(text: string): string {
  return text
    .split('\n')
    .filter((ln) => !ln.startsWith('INTERRUPT:') && !ln.startsWith('trace:'))
    .join('\n')
    .trim();
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : '';
}

function num(v: unknown): number {
  return typeof v === 'number' ? v : 0;
}

// One trace event as a terse timeline line (mirrors trace_view text).
export function traceLine(ev: TraceEvent, t0: number): string {
  const dt = num(ev.ts) - t0;
  const at = `+${dt >= 0 ? dt.toFixed(1) : '0.0'}s`;
  switch (ev.type) {
    case 'run_start':
      return `${at} START ${str(ev.script)} thread=${str(ev.thread)} model=${str(ev.model)}`;
    case 'model_request':
      return `${at} LLM req n_msgs=${num(ev.n_messages)}`;
    case 'model_response': {
      const u = (ev.usage ?? {}) as Record<string, unknown>;
      return `${at} LLM resp in=${num(u.input_tokens)} out=${num(u.output_tokens)} tot=${num(u.total_tokens)}`;
    }
    case 'tool_call': {
      const dur = num(ev.duration_s).toFixed(2);
      if (ev.ok) {
        return `${at} ok ${str(ev.name)} ${dur}s ${str(ev.args_preview)} -> ${str(ev.result_preview)}`.slice(0, 200);
      }
      return `${at} ERR ${str(ev.name)} ${dur}s err=${str(ev.error)}`.slice(0, 200);
    }
    case 'run_end':
      return `${at} END ok=${String(ev.ok)} dur=${num(ev.duration_s).toFixed(2)}s`;
    default:
      return `${at} ? ${ev.type}`;
  }
}

const NARROW_VIEWPORT_QUERY = '(max-width: 767px)';

export interface TraceSummary {
  action: string;
  timing: string;
  status: 'ok' | 'failed' | 'info';
}

function offsetClock(ev: TraceEvent, t0: number): string {
  const dt = num(ev.ts) - t0;
  return `+${dt >= 0 ? dt.toFixed(1) : '0.0'}s`;
}

// Collapsed summary row for narrow viewports: action + timing +
// ok/failed status, derived from existing event fields only.
export function traceSummary(ev: TraceEvent, t0: number): TraceSummary {
  const at = offsetClock(ev, t0);
  switch (ev.type) {
    case 'run_start':
      return { action: `START ${str(ev.script) || '?'}`, timing: at, status: 'info' };
    case 'model_request':
      return { action: 'LLM req', timing: at, status: 'info' };
    case 'model_response':
      return { action: 'LLM resp', timing: at, status: 'info' };
    case 'tool_call': {
      const dur = typeof ev.duration_s === 'number' ? `${ev.duration_s.toFixed(2)}s` : at;
      const status = ev.ok === true ? 'ok' : ev.ok === false ? 'failed' : 'info';
      return { action: str(ev.name) || '?', timing: dur, status };
    }
    case 'run_end': {
      const dur = typeof ev.duration_s === 'number' ? `${ev.duration_s.toFixed(2)}s` : at;
      const status = ev.ok === true ? 'ok' : ev.ok === false ? 'failed' : 'info';
      return { action: 'END', timing: dur, status };
    }
    default:
      return { action: `? ${ev.type}`, timing: at, status: 'info' };
  }
}

function useIsNarrowViewport(): boolean {
  const [narrow, setNarrow] = useState(false);
  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return;
    const mq = window.matchMedia(NARROW_VIEWPORT_QUERY);
    setNarrow(mq.matches);
    const onChange = (e: MediaQueryListEvent) => setNarrow(e.matches);
    if (typeof mq.addEventListener === 'function') {
      mq.addEventListener('change', onChange);
      return () => mq.removeEventListener('change', onChange);
    }
    mq.addListener(onChange);
    return () => mq.removeListener(onChange);
  }, []);
  return narrow;
}

// Run-timeline trace: the full expanded log on desktop, collapsed
// per-event summary rows (tap to expand inline) on narrow viewports.
// The narrow prop overrides the media query (used by tests).
export function TraceTimeline({
  trace,
  t0,
  narrow,
}: {
  trace: TraceEvent[];
  t0: number;
  narrow?: boolean;
}) {
  const queryNarrow = useIsNarrowViewport();
  const isNarrow = narrow ?? queryNarrow;
  if (!isNarrow) {
    return (
      <pre className="row-meta">
        {trace.length > 0
          ? trace.map((ev) => traceLine(ev, t0)).join('\n')
          : 'No trace events recorded.'}
      </pre>
    );
  }
  if (trace.length === 0) {
    return <p className="row-meta">No trace events recorded.</p>;
  }
  return (
    <ol className="trace-fold" data-testid="chat-trace-fold">
      {trace.map((ev, i) => {
        const s = traceSummary(ev, t0);
        const dotCls = s.status === 'ok' ? 'dot-ok' : s.status === 'failed' ? 'dot-bad' : '';
        return (
          // Trace events carry no ids; order is stable.
          // eslint-disable-next-line react/no-array-index-key
          <li key={i} className="trace-fold-row">
            <details>
              <summary>
                <span className={`dot ${dotCls}`} aria-hidden="true" />
                <span className="trace-fold-action">{s.action}</span>
                <span className="trace-fold-timing">{s.timing}</span>
                {s.status !== 'info' && (
                  <span className={`trace-fold-status ${s.status}`}>{s.status}</span>
                )}
              </summary>
              <pre className="row-meta">{traceLine(ev, t0)}</pre>
            </details>
          </li>
        );
      })}
    </ol>
  );
}

function runMeta(run: AgentRun): string {
  const when = run.started_ts
    ? new Date(run.started_ts * 1000).toLocaleString()
    : 'unknown time';
  const tools = run.tools.length > 0 ? ` · tools ${run.tools.join(',')}` : '';
  return `${run.script} · ${run.model} · ${run.ok ? 'ok' : 'FAILED'} · ${run.duration_s}s · ${run.tokens} tok · ${runCost(run)}${tools} · ${when}`;
}

// Compact token count for spoken labels: 11089 -> 11k, 96 -> 96.
function compactTokens(n: number): string {
  return n >= 1000 ? `${Math.round(n / 1000)}k` : `${n}`;
}

// Concise accessible name for a Past runs card button. The card's
// full preview + metadata line used to be the name (~250 chars per
// Tab stop); the label names id, status, duration, tokens, and day
// instead. Card text stays visible (and browse-readable): the label
// only replaces the Tab-stop name, so nothing is aria-hidden.
export function runButtonLabel(run: AgentRun): string {
  const when = run.started_ts
    ? new Date(run.started_ts * 1000).toISOString().slice(0, 10)
    : 'unknown time';
  return `Run ${run.id}, ${run.ok ? 'ok' : 'failed'}, ${run.duration_s}s, ${compactTokens(run.tokens)} tokens, ${when}`;
}

// Dollars trimmed for phone lines: $5 not $5.0000, $0.0008 kept.
function usd(n: number): string {
  return `$${n.toFixed(4).replace(/\.?0+$/, '')}`;
}

// Per-row cost slot: local runs are free, metered runs show the
// recorded/derived quote, unrated metered runs stay honest with a
// bare tag instead of an invented number.
export function runCost(run: AgentRun): string {
  if (!run.metered) return 'free';
  if (run.cost_usd === null || run.cost_usd === undefined) return 'metered';
  return usd(run.cost_usd);
}

// Pre-send composer line from a route hint: backend + cost signal.
// Same vocabulary as runCost (free / metered / $), with the card
// per-1M range since usage is unknown before generation. Failures
// stay visible but short: signal only, never a send block.
export function hintLine(hint: RouteHintResponse): string {
  if (!hint.ok) {
    const err = hint.error.trim().slice(0, 120) || 'unknown reason';
    return `Next reply unavailable (${err})`;
  }
  const via = `Next reply via ${hint.provider}:${hint.model}`;
  if (!hint.metered) return `${via} · free`;
  if (hint.rate_in_per_1m === null || hint.rate_out_per_1m === null) {
    return `${via} · metered`;
  }
  return `${via} · metered ~${usd(hint.rate_in_per_1m)}–${usd(hint.rate_out_per_1m)}/1M`;
}

// Remaining-budget line for the runs header. Uncapped is stated
// plainly (an unset cap is itself useful to see); a hit cap names
// the block and the figures.
export function budgetLine(budget: BudgetStatus): string {
  if (budget.cap_usd === null || budget.cap_usd === undefined) {
    return 'No spend cap set';
  }
  if (budget.blocked) {
    return `Budget cap hit — metered calls blocked (${usd(budget.spent_usd)} of ${usd(budget.cap_usd)} used)`;
  }
  return `Budget ${usd(budget.remaining_usd ?? 0)} left of ${usd(budget.cap_usd)}`;
}

export interface MeteredTotal {
  total: number;
  unknown: number;
  free: number;
}

// Running metered total over the listed runs. Rounded to whole
// microdollars so the total matches the row sums exactly.
export function meteredTotal(runs: AgentRun[]): MeteredTotal {
  let total = 0;
  let unknown = 0;
  let free = 0;
  for (const r of runs) {
    if (!r.metered) {
      free += 1;
    } else if (r.cost_usd === null || r.cost_usd === undefined) {
      unknown += 1;
    } else {
      total += r.cost_usd;
    }
  }
  return { total: Math.round(total * 1e6) / 1e6, unknown, free };
}

// One quiet Past-runs refetch after a completed send: the backend
// has recorded a new run, so the list would otherwise stay stale
// until a remount. The fresh list replaces, never appends (no
// duplicate rows); failures stay silent — a stale list beats a
// bogus chat error.
export async function refreshRunsQuiet(
  fetchRuns: () => Promise<{ runs: AgentRun[] }>,
  setRuns: (runs: AgentRun[]) => void,
): Promise<void> {
  try {
    setRuns((await fetchRuns()).runs);
  } catch {
    // Past runs stay as they were; live chat still works.
  }
}

export interface RunDecision {
  what: string;
  why: string;
}

// Per-turn what+why lines deep_chat records into extra.decisions.
// Older rows predate them; malformed entries never break the pane.
export function runDecisions(entry: Record<string, unknown>): RunDecision[] {
  const extra = entry.extra as Record<string, unknown> | undefined;
  const raw = extra?.decisions;
  if (!Array.isArray(raw)) return [];
  return raw
    .filter(
      (d): d is { what: unknown; why: unknown } =>
        typeof d === 'object' && d !== null,
    )
    .filter((d) => typeof d.what === 'string' && typeof d.why === 'string')
    .map((d) => ({ what: d.what as string, why: d.why as string }));
}

// Expanded detail for one past run: decisions + full trace.
// Rendered as the list item right after its row so the trace lands
// adjacent to the click target instead of below the whole list.
function RunDetail({ detail, t0 }: { detail: AgentRunDetail; t0: number }) {
  return (
    <li className="detail" data-testid="chat-run-detail">
      <span className="row-title">{detail.id}</span>
      {runDecisions(detail.entry).length > 0 && (
        <div data-testid="chat-run-decisions">
          <span className="row-title">Decisions</span>
          <ul className="session-list">
            {runDecisions(detail.entry).map((d, i) => (
              <li key={i} data-testid={`chat-run-decision-${i}`}>
                <span>{d.what}</span>{' '}
                <span className="row-meta">{d.why}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      <TraceTimeline trace={detail.trace} t0={t0} />
    </li>
  );
}

export function RunsPane({
  runs,
  selected,
  detail,
  onSelect,
  onContinue,
  onDelete,
  onClearFinished,
  notice,
  budget,
}: {
  runs: AgentRun[];
  selected: string | null;
  detail: AgentRunDetail | null;
  onSelect: (id: string) => void;
  onContinue: (id: string) => void;
  onDelete: (id: string) => void;
  onClearFinished: () => void;
  notice: string | null;
  budget: BudgetStatus | null;
}) {
  if (runs.length === 0) return null;
  const t0 = detail && detail.trace.length > 0 ? num(detail.trace[0].ts) : 0;
  const metered = meteredTotal(runs);
  return (
    <section data-testid="chat-runs">
      <h3>Past runs</h3>
      <p className="row-meta" data-testid="chat-runs-metered-total">
        Metered total {usd(metered.total)}
        {metered.unknown > 0 &&
          ` (+${metered.unknown} unknown rate${metered.unknown === 1 ? '' : 's'})`}
      </p>
      {budget && (
        <p className="row-meta" data-testid="chat-runs-budget">
          {budgetLine(budget)}
        </p>
      )}
      <div className="actions">
        <TapToFire
          label="Clear finished"
          confirm="This clears all finished runs."
          testid="chat-runs-clear"
          onFire={onClearFinished}
        />
      </div>
      {notice && (
        <p className="row-meta" data-testid="chat-runs-notice" role="status">
          {notice}
        </p>
      )}
      <ul className="session-list">
        {runs.map((r) => (
          <Fragment key={r.id}>
            <li className={`detail${selected === r.id ? ' selected' : ''}`}>
              <button
                type="button"
                data-testid={`chat-run-${r.id}`}
                aria-label={runButtonLabel(r)}
                aria-pressed={selected === r.id}
                onClick={() => onSelect(r.id)}
              >
                <span className="row-title">{r.preview || r.id}</span>{' '}
                <span className="row-meta">
                  {r.id} · {runMeta(r)}
                </span>
              </button>
              <div className="actions">
                <button
                  type="button"
                  data-testid={`chat-run-continue-${r.id}`}
                  disabled={!r.has_prompt}
                  title={r.has_prompt ? undefined : 'No stored prompt for this run'}
                  onClick={() => onContinue(r.id)}
                >
                  Continue
                </button>
                <TapToFire
                  label="Delete"
                  confirm="This cannot be undone."
                  testid={`chat-run-delete-${r.id}`}
                  onFire={() => onDelete(r.id)}
                />
              </div>
            </li>
            {selected === r.id && detail && <RunDetail detail={detail} t0={t0} />}
          </Fragment>
        ))}
      </ul>
    </section>
  );
}

function requestSummary(args: Record<string, unknown>): string {
  if (args.kind === 'td') return `Run: ${str(args.command)}`;
  if (args.kind === 'issue') return `File issue: ${str(args.title)}`;
  return 'Proposed write';
}

export function ApprovalCard({
  approval,
  decided,
  onDecide,
}: {
  approval: ApprovalPayload;
  decided: string | null;
  onDecide: (decision: 'approve' | 'reject') => void;
}) {
  const reqs = approval.action_requests ?? [];
  return (
    <div className="detail" data-testid="chat-approval" role="alert">
      <span className="row-title">Approval needed</span>
      <ul className="session-list">
        {reqs.map((r, i) => (
          // eslint-disable-next-line react/no-array-index-key
          <li key={i} className="row-meta">
            {requestSummary(r.args ?? {})}
          </li>
        ))}
      </ul>
      {(approval.proposals ?? []).map((p, i) => (
        // eslint-disable-next-line react/no-array-index-key
        <pre key={i} className="row-meta">
          {p}
        </pre>
      ))}
      {decided ? (
        <p className="row-meta" data-testid="chat-approval-decided">
          {decided}
        </p>
      ) : (
        <div className="actions">
          <HoldToConfirm label="Approve" onFire={() => onDecide('approve')} />
          <button type="button" data-testid="chat-reject" onClick={() => onDecide('reject')}>
            Reject
          </button>
        </div>
      )}
    </div>
  );
}

// True while the stream is open but no token has landed yet: the
// assistant placeholder is still empty, so show a typing indicator.
export function awaitingFirstToken(
  streaming: boolean,
  messages: ChatMessage[],
): boolean {
  if (!streaming || messages.length === 0) return false;
  const last = messages[messages.length - 1];
  return last.role === 'assistant' && last.text.length === 0;
}

export function TypingIndicator() {
  return (
    <li
      className="detail typing"
      data-testid="chat-typing"
      aria-live="polite"
      aria-label="Assistant is typing"
    >
      <span className="row-title">Assistant</span>
      <span className="typing-dots" aria-hidden="true">
        <span />
        <span />
        <span />
      </span>
    </li>
  );
}

// No-event watchdog: abort a stream that goes silent this long. Routine
// local prefill exceeds a minute; the backend bounds nothing on this
// path, so 3x that margin separates stall from slowness.
export const STALL_SECONDS = 180;

export function isStalled(lastEventMs: number, nowMs: number): boolean {
  return nowMs - lastEventMs > STALL_SECONDS * 1000;
}

// Retry re-sends the last prompt after a failed stream (deploy cut the
// connection, backend hiccup). Shown only when there is something to
// re-send and nothing running — never auto-runs.
export function shouldShowChatRetry(
  error: string | null,
  streaming: boolean,
  lastSent: string,
): boolean {
  return (
    !streaming && error !== null && error.length > 0 && lastSent.trim().length > 0
  );
}

// Streaming progress line: elapsed always, plus phase and token count
// once known. "Working… 45s · prefill · 12 tok".
export function streamProgress(
  elapsedSec: number,
  phase: string | null,
  tokens: number,
): string {
  let line = `Working… ${elapsedSec}s`;
  if (phase) line += ` · ${phase}`;
  if (tokens > 0) line += ` · ${tokens} tok`;
  return line;
}

// Ticks every second while streaming, so deliberately not a live
// region (the sr-only phase announcer carries screen-reader updates).
export function StreamProgress({
  elapsed,
  phase,
  tokens,
}: {
  elapsed: number;
  phase: string | null;
  tokens: number;
}) {
  return (
    <p className="row-meta" data-testid="chat-progress">
      {streamProgress(elapsed, phase, tokens)}
    </p>
  );
}

function newSessionId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return crypto.randomUUID().replace(/-/g, '').slice(0, 12);
  }
  return Math.random().toString(16).slice(2, 14).padEnd(12, '0');
}

interface SseEvent {
  name: string;
  data: string;
}

function parseSsePart(part: string): SseEvent | null {
  const name = (part.match(/^event:\s*(.*)$/m)?.[1] ?? '').trim();
  const dataMatch = part.match(/^data:\s*(.*)$/m);
  if (!name || !dataMatch) return null;
  return { name, data: dataMatch[1] };
}

function parseSse(buffer: string): { events: SseEvent[]; rest: string } {
  const parts = buffer.split('\n\n');
  const rest = parts.pop() ?? '';
  const events = parts
    .map(parseSsePart)
    .filter((e): e is SseEvent => e !== null);
  return { events, rest };
}

function parseJson<T>(data: string): T | null {
  try {
    return JSON.parse(data) as T;
  } catch {
    return null;
  }
}

async function pumpStream(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  onToken: (token: string) => void,
  onApproval: (payload: ApprovalPayload) => void,
  onPhase: (message: string | null) => void,
  onDone: (text: string) => void,
  onEvent: () => void,
): Promise<void> {
  const decoder = new TextDecoder();
  let buf = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const { events, rest } = parseSse(buf);
    buf = rest;
    for (const ev of events) {
      onEvent();
      if (ev.name === 'error') throw new Error(ev.data);
      if (ev.name === 'token') {
        onToken(parseJson<{ token?: string }>(ev.data)?.token ?? '');
      }
      if (ev.name === 'approval') {
        const payload = parseJson<ApprovalPayload>(ev.data);
        if (payload) onApproval(payload);
      }
      if (ev.name === 'phase') {
        const msg = parseJson<{ message?: string }>(ev.data)?.message;
        onPhase(msg && msg.length > 0 ? msg : null);
      }
      if (ev.name === 'done') {
        onPhase(null);
        onDone(parseJson<{ text?: string }>(ev.data)?.text ?? '');
      }
    }
  }
}

export default function ChatView({
  repo,
  branch,
  seed,
  onSeedUsed,
  initialRuns,
}: {
  repo: string;
  branch?: string;
  seed: string | null;
  onSeedUsed: () => void;
  // Test seam (drawer-precedented): pins the composer-above-runs
  // order without waiting on the runs fetch.
  initialRuns?: AgentRun[];
}) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [prompt, setPrompt] = useState(seed ?? '');
  // One tap on Ask agent anywhere lands here with a seed and the agent
  // starts working immediately; the ref survives StrictMode's double
  // effect-invoke so dev never double-sends.
  const seedSentRef = useRef(false);
  useEffect(() => {
    if (seed && !seedSentRef.current) {
      seedSentRef.current = true;
      onSeedUsed();
      void onSend();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const [streaming, setStreaming] = useState(false);
  const [phase, setPhase] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastSent, setLastSent] = useState('');
  const [runs, setRuns] = useState<AgentRun[]>(initialRuns ?? []);
  const [budget, setBudget] = useState<BudgetStatus | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<AgentRunDetail | null>(null);
  const [runsNotice, setRunsNotice] = useState<string | null>(null);
  const promptRef = useRef<HTMLTextAreaElement>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const [approval, setApproval] = useState<ApprovalPayload | null>(null);
  const [decided, setDecided] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [tokenCount, setTokenCount] = useState(0);
  // Composer backend picker: per-turn override for the next reply.
  // View-level state, so the choice persists across sends for this
  // view session. Auto = today's ladder, untouched.
  const [backend, setBackend] = useState<ChatBackend>('auto');
  // Pre-send route hint for the composer line. Refetched whenever
  // the picker changes; cleared first so a stale backend never
  // shows under a new selection. Mock mode uses a canned local
  // hint (no network, deterministic ?mock=1).
  const [hint, setHint] = useState<RouteHintResponse | null>(null);
  const sessionRef = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    if (useMock()) {
      setHint({
        ok: true,
        provider: 'ollama',
        model: 'qwen3:8b',
        metered: false,
        rate_in_per_1m: null,
        rate_out_per_1m: null,
        error: '',
      });
      return;
    }
    setHint(null);
    fetchRouteHint(backend).then(
      (h) => {
        if (!cancelled) setHint(h);
      },
      () => {
        if (!cancelled) {
          setHint({
            ok: false,
            provider: '',
            model: '',
            metered: false,
            rate_in_per_1m: null,
            rate_out_per_1m: null,
            error: 'hint unavailable',
          });
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [backend]);
  const abortRef = useRef<AbortController | null>(null);
  const startRef = useRef(0);
  const lastEventRef = useRef(0);
  const stalledRef = useRef(false);

  useEffect(() => {
    if (useMock()) {
      setMessages(loadChat().messages);
      return;
    }
    (async () => {
      try {
        const live = await fetchAgentRuns();
        setRuns(live.runs);
      } catch {
        // Past runs stay hidden; live chat still works.
      }
      try {
        setBudget(await fetchBudget());
      } catch {
        // Budget line stays hidden; runs still render.
      }
    })();
  }, []);

  async function onSelectRun(id: string) {
    setSelected(id);
    setDetail(null);
    try {
      setDetail(await fetchAgentRun(id));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function refreshRuns() {
    try {
      setRuns((await fetchAgentRuns()).runs);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function onContinueRun(id: string) {
    try {
      const d = selected === id && detail ? detail : await fetchAgentRun(id);
      const extra = d.entry.extra as Record<string, unknown> | undefined;
      const stored = str(extra?.prompt);
      if (!stored) {
        setRunsNotice('That run has no stored prompt to continue from.');
        return;
      }
      setPrompt(stored);
      setRunsNotice(null);
      promptRef.current?.focus();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function onDeleteRun(id: string) {
    try {
      await deleteAgentRun(id);
      if (selected === id) {
        setSelected(null);
        setDetail(null);
      }
      await refreshRuns();
      setRunsNotice(`Deleted ${id}.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function onClearFinished() {
    try {
      const result = await clearAgentRuns();
      setSelected(null);
      setDetail(null);
      await refreshRuns();
      setRunsNotice(`Cleared ${result.cleared} finished run(s).`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  function appendToken(token: string) {
    if (!token) return;
    setTokenCount((c) => c + 1);
    setMessages((m) => {
      const next = [...m];
      const last = next[next.length - 1];
      next[next.length - 1] = { role: 'assistant', text: last.text + token };
      return next;
    });
  }

  function backfillDone(text: string) {
    if (!text) return;
    setMessages((m) => {
      const next = [...m];
      const last = next[next.length - 1];
      if (!last || last.role !== 'assistant' || last.text.length > 0) return m;
      next[next.length - 1] = { role: 'assistant', text };
      return next;
    });
  }

  async function streamLive(text: string, sessionId: string) {
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const resp = await fetch(chatStreamUrl(text, repo, sessionId, backend), {
        signal: ctrl.signal,
      });
      if (!resp.ok || !resp.body) throw new Error(`Chat stream failed: ${resp.status}`);
      await pumpStream(
        resp.body.getReader(),
        appendToken,
        (payload) => {
          setApproval(payload);
          setDecided(null);
        },
        setPhase,
        backfillDone,
        () => {
          lastEventRef.current = Date.now();
        },
      );
    } catch (err) {
      if ((err as Error).name !== 'AbortError') throw err;
    } finally {
      abortRef.current = null;
    }
  }

  function mockReply() {
    const text = loadChat().messages.find((x) => x.role === 'assistant')?.text ?? 'Mock reply.';
    setMessages((m) => [...m, { role: 'assistant', text }]);
  }

  async function sendText(text: string) {
    setError(null);
    setPhase(null);
    setApproval(null);
    setDecided(null);
    setLastSent(text);
    const sessionId = newSessionId();
    sessionRef.current = sessionId;
    setMessages((m) => [...m, { role: 'user', text }]);
    setPrompt('');
    if (useMock()) {
      mockReply();
      return;
    }
    startRef.current = Date.now();
    lastEventRef.current = Date.now();
    stalledRef.current = false;
    setElapsed(0);
    setTokenCount(0);
    setStreaming(true);
    setMessages((m) => [...m, { role: 'assistant', text: '' }]);
    try {
      await streamLive(text, sessionId);
      await refreshRunsQuiet(fetchAgentRuns, setRuns);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setStreaming(false);
    }
  }

  async function onSend(e?: React.FormEvent) {
    e?.preventDefault();
    const text = prompt.trim();
    if (!text || streaming) return;
    await sendText(text);
  }

  function onRetry() {
    if (!lastSent.trim() || streaming) return;
    void sendText(lastSent);
  }

  function stopStream() {
    abortRef.current?.abort();
    abortRef.current = null;
    setStreaming(false);
    setPhase(null);
  }

  async function onAbort() {
    stopStream();
    const sid = sessionRef.current;
    if (!sid || useMock()) return;
    try {
      await abortChat(sid);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  function onStall() {
    stopStream();
    const sid = sessionRef.current;
    setError(`Stream stalled — no response for ${STALL_SECONDS}s.`);
    if (!sid || useMock()) return;
    void abortChat(sid).catch(() => {
      // Run already dead; the stall message stands.
    });
  }

  // Elapsed ticker + stall watchdog while streaming.
  useEffect(() => {
    if (!streaming) return;
    const t = window.setInterval(() => {
      setElapsed(Math.floor((Date.now() - startRef.current) / 1000));
      if (!stalledRef.current && isStalled(lastEventRef.current, Date.now())) {
        stalledRef.current = true;
        onStall();
      }
    }, 1000);
    return () => window.clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [streaming]);

  async function onDecide(decision: 'approve' | 'reject') {
    if (!approval || decided) return;
    setDecided(decision === 'approve' ? 'Approved — running…' : 'Rejected.');
    try {
      const result = await resumeChat(approval.session_id, decision);
      setMessages((m) => [...m, { role: 'assistant', text: result.text }]);
      if (result.approval) {
        setApproval(result.approval);
        setDecided(null);
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      if (/: 409\b/.test(msg)) {
        setDecided('Already decided.');
      } else {
        setError(msg);
        setDecided(null);
      }
    }
  }

  const typing = awaitingFirstToken(streaming, messages);
  const shown = typing ? messages.slice(0, -1) : messages;

  // Pin the live reply in view while streaming: the reply renders
  // at the list end and used to demand a manual scroll back up past
  // the runs after every send (#594). Nearest-scroll only moves when
  // the tail is actually out of view.
  useEffect(() => {
    if (!streaming) return;
    bottomRef.current?.scrollIntoView({ block: 'nearest' });
  }, [streaming, messages]);

  return (
    <section data-testid="chat-view">
      <header className="page-head">
        <div>
          <h2>Chat</h2>
          <RepoLabel repo={repo} branch={branch} />
        </div>
        {streaming && (
          <button type="button" data-testid="chat-abort" onClick={() => void onAbort()}>
            Abort
          </button>
        )}
      </header>
      {error && (
        <p className="row-meta" data-testid="chat-error" role="alert">
          Chat failed: {error}
        </p>
      )}
      {shouldShowChatRetry(error, streaming, lastSent) && (
        <button type="button" data-testid="chat-retry" onClick={() => onRetry()}>
          Retry
        </button>
      )}
      {streaming && <StreamProgress elapsed={elapsed} phase={phase} tokens={tokenCount} />}
      <span className="sr-only" role="status" data-testid="chat-phase">
        {streaming ? phase : null}
      </span>
      <ul className="session-list" data-testid="chat-history">
        {shown.map((m, i) => (
          <li key={i} className="detail" data-testid={`chat-msg-${m.role}-${i}`}>
            <span className="row-title">{m.role === 'user' ? 'You' : 'Assistant'}</span>
            <p>{m.role === 'assistant' ? cleanAgentText(m.text) : m.text}</p>
          </li>
        ))}
        {typing && <TypingIndicator />}
      </ul>
      <div ref={bottomRef} data-testid="chat-bottom" aria-hidden="true" />
      {messages.length === 0 && (
        <p className="row-meta" data-testid="chat-empty">
          Ask anything about this repo.
        </p>
      )}
      {approval && (
        <ApprovalCard approval={approval} decided={decided} onDecide={(d) => void onDecide(d)} />
      )}
      {/* Composer above Past runs: with 8+ runs the form used to
          drop below every run card, burying Prompt + Send (#564). */}
      <form onSubmit={(e) => void onSend(e)}>
        <label className="row-meta" htmlFor="chat-prompt">
          Prompt{' '}
          <textarea
            id="chat-prompt"
            data-testid="chat-prompt"
            ref={promptRef}
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => {
              if (isKeyboardSend(e)) {
                e.preventDefault();
                void onSend();
              }
            }}
            disabled={streaming}
          />
        </label>
        <p className="row-meta" data-testid="chat-route-hint" role="status">
          {hint ? hintLine(hint) : 'Next reply via …'}
        </p>
        <div className="actions" role="group" aria-label="Chat backend" data-testid="chat-backend-picker">
          {CHAT_BACKEND_OPTIONS.map((o) => (
            <button
              key={o.value}
              type="button"
              data-testid={`chat-backend-${o.value}`}
              aria-pressed={backend === o.value}
              disabled={streaming}
              onClick={() => setBackend(o.value)}
            >
              {o.label}
            </button>
          ))}
        </div>
        <div className="actions">
          <button
            type="submit"
            data-testid="chat-send"
            title="Send (Ctrl+Enter or Cmd+Enter)"
            disabled={streaming || !prompt.trim()}
          >
            Send
          </button>
        </div>
      </form>
      <RunsPane
        runs={runs}
        selected={selected}
        detail={detail}
        onSelect={(id) => void onSelectRun(id)}
        onContinue={(id) => void onContinueRun(id)}
        onDelete={(id) => void onDeleteRun(id)}
        onClearFinished={() => void onClearFinished()}
        notice={runsNotice}
        budget={budget}
      />
      <AgentDrawer repo={repo} />
    </section>
  );
}
