import { useEffect, useRef, useState } from 'react';
import HoldToConfirm from '../components/HoldToConfirm';
import {
  abortChat,
  chatStreamUrl,
  fetchAgentRun,
  fetchAgentRuns,
  resumeChat,
} from '../apiClient';
import type {
  AgentRun,
  AgentRunDetail,
  ApprovalPayload,
  TraceEvent,
} from '../apiClient';
import { loadChat, useMock, type ChatMessage } from './mockViews';

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

function runMeta(run: AgentRun): string {
  const when = run.started_ts
    ? new Date(run.started_ts * 1000).toLocaleString()
    : 'unknown time';
  const tools = run.tools.length > 0 ? ` · tools ${run.tools.join(',')}` : '';
  return `${run.script} · ${run.model} · ${run.ok ? 'ok' : 'FAILED'} · ${run.duration_s}s · ${run.tokens} tok${tools} · ${when}`;
}

function RunsPane({
  runs,
  selected,
  detail,
  onSelect,
}: {
  runs: AgentRun[];
  selected: string | null;
  detail: AgentRunDetail | null;
  onSelect: (id: string) => void;
}) {
  if (runs.length === 0) return null;
  const t0 = detail && detail.trace.length > 0 ? num(detail.trace[0].ts) : 0;
  return (
    <section data-testid="chat-runs">
      <h3>Past runs</h3>
      <ul className="session-list">
        {runs.map((r) => (
          <li key={r.id} className="detail">
            <button
              type="button"
              data-testid={`chat-run-${r.id}`}
              aria-pressed={selected === r.id}
              onClick={() => onSelect(r.id)}
            >
              <span className="row-title">{r.preview || r.id}</span>{' '}
              <span className="row-meta">
                {r.id} · {runMeta(r)}
              </span>
            </button>
          </li>
        ))}
      </ul>
      {selected && detail && (
        <div className="detail" data-testid="chat-run-detail">
          <span className="row-title">{detail.id}</span>
          <pre className="row-meta">
            {detail.trace.length > 0
              ? detail.trace.map((ev) => traceLine(ev, t0)).join('\n')
              : 'No trace events recorded.'}
          </pre>
        </div>
      )}
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
  seed,
  onSeedUsed,
}: {
  repo: string;
  seed: string | null;
  onSeedUsed: () => void;
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
  const [runs, setRuns] = useState<AgentRun[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<AgentRunDetail | null>(null);
  const [approval, setApproval] = useState<ApprovalPayload | null>(null);
  const [decided, setDecided] = useState<string | null>(null);
  const sessionRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

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

  function appendToken(token: string) {
    if (!token) return;
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
      const resp = await fetch(chatStreamUrl(text, repo, sessionId), { signal: ctrl.signal });
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

  async function onSend(e?: React.FormEvent) {
    e?.preventDefault();
    const text = prompt.trim();
    if (!text || streaming) return;
    setError(null);
    setPhase(null);
    setApproval(null);
    setDecided(null);
    const sessionId = newSessionId();
    sessionRef.current = sessionId;
    setMessages((m) => [...m, { role: 'user', text }]);
    setPrompt('');
    if (useMock()) {
      mockReply();
      return;
    }
    setStreaming(true);
    setMessages((m) => [...m, { role: 'assistant', text: '' }]);
    try {
      await streamLive(text, sessionId);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setStreaming(false);
    }
  }

  async function onAbort() {
    abortRef.current?.abort();
    const sid = sessionRef.current;
    setStreaming(false);
    setPhase(null);
    if (!sid || useMock()) return;
    try {
      await abortChat(sid);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

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

  return (
    <section data-testid="chat-view">
      <header className="page-head">
        <div>
          <h2>Chat</h2>
          <p className="row-meta">{repo}</p>
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
      {streaming && phase && (
        <p className="row-meta" data-testid="chat-phase">
          {phase}
        </p>
      )}
      <ul className="session-list" data-testid="chat-history">
        {messages.map((m, i) => (
          <li key={i} className="detail" data-testid={`chat-msg-${m.role}-${i}`}>
            <span className="row-title">{m.role === 'user' ? 'You' : 'Assistant'}</span>
            <p>{m.role === 'assistant' ? cleanAgentText(m.text) : m.text}</p>
          </li>
        ))}
      </ul>
      {messages.length === 0 && (
        <p className="row-meta" data-testid="chat-empty">
          Ask anything about this repo.
        </p>
      )}
      {approval && (
        <ApprovalCard approval={approval} decided={decided} onDecide={(d) => void onDecide(d)} />
      )}
      <RunsPane
        runs={runs}
        selected={selected}
        detail={detail}
        onSelect={(id) => void onSelectRun(id)}
      />
      <form onSubmit={(e) => void onSend(e)}>
        <label className="row-meta" htmlFor="chat-prompt">
          Prompt{' '}
          <textarea
            id="chat-prompt"
            data-testid="chat-prompt"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            disabled={streaming}
          />
        </label>
        <div className="actions">
          <button type="submit" data-testid="chat-send" disabled={streaming || !prompt.trim()}>
            Send
          </button>
        </div>
      </form>
    </section>
  );
}
