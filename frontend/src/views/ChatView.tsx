import { useEffect, useRef, useState } from 'react';
import {
  abortChat,
  chatStreamUrl,
  fetchAgentSessionDetail,
  fetchAgentSessions,
} from '../apiClient';
import type { AgentSession, AgentSessionDetail } from '../apiClient';
import { loadChat, useMock, type ChatMessage } from './mockViews';

function SessionPane({
  sessions,
  selected,
  detail,
  onSelect,
}: {
  sessions: AgentSession[];
  selected: string | null;
  detail: AgentSessionDetail | null;
  onSelect: (file: string) => void;
}) {
  if (sessions.length === 0) return null;
  return (
    <section data-testid="chat-sessions">
      <h3>Past sessions</h3>
      <ul className="session-list">
        {sessions.map((s) => (
          <li key={s.file} className="detail">
            <button
              type="button"
              data-testid={`chat-session-${s.file}`}
              aria-pressed={selected === s.file}
              onClick={() => onSelect(s.file)}
            >
              <span className="row-title">{s.title || s.file}</span>{' '}
              <span className="row-meta">
                {s.repo} · {new Date(s.mtime * 1000).toLocaleString()}
              </span>
            </button>
          </li>
        ))}
      </ul>
      {selected && detail && (
        <div className="detail" data-testid="chat-session-detail">
          <span className="row-title">
            {sessionDetailTitle(detail)}
          </span>
          <ul className="session-list">
            {detail.turns.slice(0, 20).map((t, i) => (
              // eslint-disable-next-line react/no-array-index-key
              <li key={i} className="detail">
                <span className="row-title">{t.role}</span>
                <p>{t.text}</p>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function formatStarted(started: unknown): string {
  if (typeof started === 'number') {
    return new Date(started > 1e12 ? started : started * 1000).toLocaleString();
  }
  if (typeof started === 'string' && started) {
    const parsed = new Date(started);
    return Number.isNaN(parsed.getTime()) ? started : parsed.toLocaleString();
  }
  return '';
}

function sessionDetailTitle(detail: AgentSessionDetail): string {
  const when = formatStarted(detail.started);
  return `${detail.repo} · ${detail.turns.length} turn(s)${when ? ` · ${when}` : ''}`;
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

function tokenOf(data: string): string {
  try {
    return (JSON.parse(data) as { token?: string }).token ?? '';
  } catch {
    return '';
  }
}

async function pumpStream(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  onToken: (token: string) => void,
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
      if (ev.name === 'token') onToken(tokenOf(ev.data));
    }
  }
}

export default function ChatView({ repo }: { repo: string }) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [prompt, setPrompt] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sessions, setSessions] = useState<AgentSession[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<AgentSessionDetail | null>(null);
  const sessionRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (useMock()) {
      setMessages(loadChat().messages);
      return;
    }
    (async () => {
      try {
        const live = await fetchAgentSessions();
        setSessions(live.sessions);
      } catch {
        // Past sessions stay hidden; live chat still works.
      }
    })();
  }, []);

  async function onSelectSession(file: string) {
    setSelected(file);
    setDetail(null);
    try {
      setDetail(await fetchAgentSessionDetail(file));
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

  async function streamLive(text: string, sessionId: string) {
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const resp = await fetch(chatStreamUrl(text, repo, sessionId), { signal: ctrl.signal });
      if (!resp.ok || !resp.body) throw new Error(`Chat stream failed: ${resp.status}`);
      await pumpStream(resp.body.getReader(), appendToken);
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
    if (!sid || useMock()) return;
    try {
      await abortChat(sid);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
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
      <ul className="session-list" data-testid="chat-history">
        {messages.map((m, i) => (
          <li key={i} className="detail" data-testid={`chat-msg-${m.role}-${i}`}>
            <span className="row-title">{m.role === 'user' ? 'You' : 'Assistant'}</span>
            <p>{m.text}</p>
          </li>
        ))}
      </ul>
      {messages.length === 0 && (
        <p className="row-meta" data-testid="chat-empty">
          Ask anything about this repo.
        </p>
      )}
      <SessionPane
        sessions={sessions}
        selected={selected}
        detail={detail}
        onSelect={(file) => void onSelectSession(file)}
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
