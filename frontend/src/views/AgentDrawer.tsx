import { useEffect, useRef, useState } from 'react';
import HoldToConfirm from '../components/HoldToConfirm';
import {
  abortAgentSession,
  agentWsUrl,
  fetchAgentProviders,
  spawnAgentSession,
} from '../apiClient';
import { useMock } from './mockViews';

export const TRANSCRIPT_MAX = 20000;

export function appendCapped(prev: string, chunk: string): string {
  const next = prev + chunk;
  return next.length > TRANSCRIPT_MAX ? next.slice(-TRANSCRIPT_MAX) : next;
}

type Phase = 'idle' | 'live' | 'closed';

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
  const [phase, setPhase] = useState<Phase>('idle');
  const [transcript, setTranscript] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [exit, setExit] = useState<number | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const sidRef = useRef<string | null>(null);

  useEffect(() => {
    if (initialProviders) return;
    if (useMock()) {
      setProviders(['muse', 'agy', 'pi']);
      return;
    }
    (async () => {
      try {
        const live = await fetchAgentProviders();
        setProviders(live.providers);
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
      // Server reaps the session when the socket closes.
      try {
        wsRef.current?.close();
      } catch {
        /* already gone */
      }
    },
    [],
  );

  function pushChunk(chunk: string) {
    if (!chunk) return;
    setTranscript((t) => appendCapped(t, chunk));
  }

  async function onLaunch() {
    if (!provider || phase === 'live') return;
    setError(null);
    setExit(null);
    setTranscript('');
    if (useMock() && !initialProviders) {
      setPhase('live');
      setTranscript('Mock agent transcript.\n');
      return;
    }
    const text = prompt.trim();
    try {
      const { session_id } = await spawnAgentSession(provider, repo);
      sidRef.current = session_id;
      const ws = new WebSocket(
        agentWsUrl(window.location.protocol, window.location.host, session_id),
      );
      wsRef.current = ws;
      ws.onopen = () => {
        if (text) ws.send(`${text}\n`);
      };
      ws.onmessage = (ev) => {
        pushChunk(typeof ev.data === 'string' ? ev.data : '');
      };
      ws.onclose = () => {
        setPhase('closed');
      };
      ws.onerror = () => {
        setError('Agent connection failed.');
        setPhase('closed');
      };
      setPhase('live');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function onAbort() {
    const sid = sidRef.current;
    try {
      wsRef.current?.close();
    } catch {
      /* already gone */
    }
    wsRef.current = null;
    setPhase('closed');
    if (!sid) return;
    try {
      const result = await abortAgentSession(sid);
      setExit(result.exit);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      sidRef.current = null;
    }
  }

  async function onFollowup(e: React.FormEvent) {
    e.preventDefault();
    const ws = wsRef.current;
    const text = prompt.trim();
    if (!ws || ws.readyState !== WebSocket.OPEN || !text) return;
    ws.send(`${text}\n`);
    setPrompt('');
  }

  return (
    <section data-testid="agent-drawer">
      <h3>Agent session</h3>
      {error && (
        <p className="row-meta" data-testid="agent-error" role="alert">
          Agent failed: {error}
        </p>
      )}
      {phase !== 'live' && (
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
            Prompt{' '}
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
          {phase === 'closed' && (
            <p className="row-meta" data-testid="agent-exit">
              Session ended{exit === null || exit === undefined ? '' : ` (exit ${exit})`}.
            </p>
          )}
        </div>
      )}
      {phase === 'live' && (
        <div className="detail">
          <span className="row-title">
            {provider} · {repo}
          </span>
          <pre className="row-meta" data-testid="agent-transcript">
            {transcript || 'Starting session…'}
          </pre>
          <form onSubmit={(e) => void onFollowup(e)}>
            <label className="row-meta" htmlFor="agent-followup">
              Send{' '}
              <textarea
                id="agent-followup"
                data-testid="agent-followup"
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
              />
            </label>
            <div className="actions">
              <button type="submit" data-testid="agent-send" disabled={!prompt.trim()}>
                Send
              </button>
              <button type="button" data-testid="agent-abort" onClick={() => void onAbort()}>
                Abort
              </button>
            </div>
          </form>
        </div>
      )}
    </section>
  );
}
