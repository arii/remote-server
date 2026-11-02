import { useEffect, useRef, useState } from 'react';
import { abortChat, chatStreamUrl } from '../apiClient';
import { loadChat, useMock, type ChatMessage } from './mockViews';

function newSessionId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return crypto.randomUUID().replace(/-/g, '').slice(0, 12);
  }
  return Math.random().toString(16).slice(2, 14).padEnd(12, '0');
}

function parseSse(buffer: string): { events: Array<{ name: string; data: string }>; rest: string } {
  const events: Array<{ name: string; data: string }> = [];
  const parts = buffer.split('\n\n');
  const rest = parts.pop() ?? '';
  for (const part of parts) {
    const name = (part.match(/^event:\s*(.*)$/m)?.[1] ?? '').trim();
    const dataMatch = part.match(/^data:\s*(.*)$/m);
    if (name && dataMatch) events.push({ name, data: dataMatch[1] });
  }
  return { events, rest };
}

export default function ChatView({ repo }: { repo: string }) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [prompt, setPrompt] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const sessionRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (useMock()) setMessages(loadChat().messages);
  }, []);

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
      setMessages((m) => [...m, { role: 'assistant', text: loadChat().messages.find((x) => x.role === 'assistant')?.text ?? 'Mock reply.' }]);
      return;
    }
    setStreaming(true);
    setMessages((m) => [...m, { role: 'assistant', text: '' }]);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const resp = await fetch(chatStreamUrl(text, repo, sessionId), { signal: ctrl.signal });
      if (!resp.ok || !resp.body) throw new Error(`Chat stream failed: ${resp.status}`);
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const { events, rest } = parseSse(buf);
        buf = rest;
        for (const ev of events) {
          if (ev.name === 'token') {
            try {
              const tok = (JSON.parse(ev.data) as { token?: string }).token ?? '';
              setMessages((m) => {
                const next = [...m];
                next[next.length - 1] = {
                  role: 'assistant',
                  text: next[next.length - 1].text + tok,
                };
                return next;
              });
            } catch {
              /* ignore malformed token frame */
            }
          } else if (ev.name === 'error') {
            throw new Error(ev.data);
          }
        }
      }
    } catch (err) {
      if ((err as Error).name !== 'AbortError') {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
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
