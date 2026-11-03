import { useEffect, useRef, useState } from 'react';
import HoldToConfirm from '../components/HoldToConfirm';
import { abortAgentSession, fetchAgentProviders, postAgentTurn } from '../apiClient';
import { useMock } from './mockViews';

interface Turn {
  role: 'user' | 'assistant';
  text: string;
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

// Pump one turn's SSE stream: token chunks append to the live answer,
// done resolves {session id, full text}, error rejects with the detail.
export async function pumpTurn(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  onToken: (token: string) => void,
): Promise<{ sessionId: string; text: string }> {
  const decoder = new TextDecoder();
  let buf = '';
  let sessionId = '';
  let text = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const parts = buf.split('\n\n');
    buf = parts.pop() ?? '';
    for (const part of parts) {
      const ev = parseSsePart(part);
      if (!ev) continue;
      if (ev.name === 'error') {
        let detail = ev.data;
        try {
          detail = (JSON.parse(ev.data) as { error?: string }).error ?? ev.data;
        } catch {
          /* raw text */
        }
        throw new Error(detail);
      }
      if (ev.name === 'token') {
        try {
          onToken((JSON.parse(ev.data) as { token?: string }).token ?? '');
        } catch {
          /* partial frame; next chunk completes it */
        }
      }
      if (ev.name === 'done') {
        try {
          const data = JSON.parse(ev.data) as { session_id?: string; text?: string };
          if (data.session_id) sessionId = data.session_id;
          if (data.text) text = data.text;
        } catch {
          /* ignore */
        }
      }
    }
  }
  return { sessionId, text };
}

export default function AgentDrawer({
  repo,
  initialProviders,
}: {
  repo: string;
  initialProviders?: string[];
}) {
  const [providers, setProviders] = useState<string[]>(initialProviders ?? []);
  const [provider, setProvider] = useState('');
  const [prompt, setPrompt] = useState('');
  const [turns, setTurns] = useState<Turn[]>([]);
  const [live, setLive] = useState(false);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const sidRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (initialProviders) return;
    if (useMock()) {
      setProviders(['muse', 'agy']);
      return;
    }
    (async () => {
      try {
        const liveProviders = await fetchAgentProviders();
        setProviders(liveProviders.providers);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    })();
  }, [initialProviders]);

  useEffect(() => {
    if (!provider && providers.length > 0) setProvider(providers[0]);
  }, [provider, providers]);

  useEffect(
    () => () => {
      abortRef.current?.abort();
    },
    [],
  );

  const streamedRef = useRef(0);

  function appendAnswer(token: string) {
    if (!token) return;
    streamedRef.current += token.length;
    setTurns((t) => {
      const next = [...t];
      const last = next[next.length - 1];
      if (!last || last.role !== 'assistant') return t;
      next[next.length - 1] = { role: 'assistant', text: last.text + token };
      return next;
    });
  }

  function backfillAnswer(text: string) {
    // The CLI buffered and no token frames streamed: fill the bubble.
    if (!text || streamedRef.current > 0) return;
    setTurns((t) => {
      const next = [...t];
      const last = next[next.length - 1];
      if (!last || last.role !== 'assistant') return t;
      next[next.length - 1] = { role: 'assistant', text };
      return next;
    });
  }

  async function sendTurn(text: string) {
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setWorking(true);
    setTurns((t) => [...t, { role: 'assistant', text: '' }]);
    try {
      if (useMock() && !initialProviders) {
        appendAnswer('Mock agent reply.');
        return;
      }
      const resp = await postAgentTurn(provider, repo, text, sidRef.current, ctrl.signal);
      if (!resp.ok || !resp.body) throw new Error(`Agent turn failed: ${resp.status}`);
      streamedRef.current = 0;
      const { sessionId, text: full } = await pumpTurn(resp.body.getReader(), appendAnswer);
      if (sessionId) sidRef.current = sessionId;
      backfillAnswer(full);
    } catch (err) {
      if ((err as Error).name !== 'AbortError') {
        setError(err instanceof Error ? err.message : String(err));
        // Drop the empty assistant bubble so retry sends cleanly.
        setTurns((t) => {
          const last = t[t.length - 1];
          if (last && last.role === 'assistant' && !last.text) return t.slice(0, -1);
          return t;
        });
      }
    } finally {
      abortRef.current = null;
      setWorking(false);
    }
  }

  async function onLaunch() {
    if (!provider || live) return;
    const text = prompt.trim();
    if (!text) {
      setError('Describe the task first.');
      return;
    }
    setError(null);
    setTurns([{ role: 'user', text }]);
    setPrompt('');
    setLive(true);
    await sendTurn(text);
  }

  async function onFollowup(e: React.FormEvent) {
    e.preventDefault();
    const text = prompt.trim();
    if (working || !text) return;
    setError(null);
    setTurns((t) => [...t, { role: 'user', text }]);
    setPrompt('');
    await sendTurn(text);
  }

  async function onAbort() {
    abortRef.current?.abort();
    const sid = sidRef.current;
    setWorking(false);
    if (!sid || (useMock() && !initialProviders)) return;
    try {
      await abortAgentSession(sid);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  function onNewSession() {
    abortRef.current?.abort();
    sidRef.current = null;
    setTurns([]);
    setError(null);
    setWorking(false);
    setLive(false);
  }

  return (
    <section data-testid="agent-drawer">
      <h3>Agent session</h3>
      {error && (
        <p className="row-meta" data-testid="agent-error" role="alert">
          Agent failed: {error}
        </p>
      )}
      {!live && (
        <div className="detail">
          <label className="row-meta" htmlFor="agent-provider">
            Provider{' '}
            <select
              id="agent-provider"
              data-testid="agent-provider"
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
            >
              {providers.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </label>
          <label className="row-meta" htmlFor="agent-prompt">
            Task{' '}
            <textarea
              id="agent-prompt"
              data-testid="agent-prompt"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="Task for the agent (sent on launch)"
            />
          </label>
          <div className="actions">
            <HoldToConfirm
              label="Launch"
              disabled={providers.length === 0}
              onFire={() => void onLaunch()}
            />
          </div>
        </div>
      )}
      {live && (
        <div className="detail">
          <span className="row-title">
            {provider} · {repo}
          </span>
          <ul className="session-list" data-testid="agent-transcript">
            {turns.map((t, i) => (
              // eslint-disable-next-line react/no-array-index-key
              <li key={i} className="detail" data-testid={`agent-msg-${t.role}-${i}`}>
                <span className="row-title">{t.role === 'user' ? 'You' : provider}</span>
                <p>{t.text || (working && i === turns.length - 1 ? 'Working…' : '')}</p>
              </li>
            ))}
          </ul>
          <form onSubmit={(e) => void onFollowup(e)}>
            <label className="row-meta" htmlFor="agent-followup">
              Send{' '}
              <textarea
                id="agent-followup"
                data-testid="agent-followup"
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                disabled={working}
              />
            </label>
            <div className="actions">
              <button type="submit" data-testid="agent-send" disabled={working || !prompt.trim()}>
                Send
              </button>
              {working && (
                <button type="button" data-testid="agent-abort" onClick={() => void onAbort()}>
                  Abort
                </button>
              )}
              <button type="button" data-testid="agent-new" onClick={onNewSession}>
                New session
              </button>
            </div>
          </form>
        </div>
      )}
    </section>
  );
}
