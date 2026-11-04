import { useEffect, useRef, useState } from 'react';
import TapToFire from '../components/TapToFire';
import { abortAgentSession, fetchAgentProviders, postAgentTurn } from '../apiClient';
import { useMock } from './mockViews';

// Mirrors backend PROVIDERS (agent_turn.py): the picker renders
// these instantly and live status refines them.
const FALLBACK_PROVIDERS = ['muse', 'agy'];

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
// done resolves {session id, full text, truncated}, error rejects.
export async function pumpTurn(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  onToken: (token: string) => void,
): Promise<{ sessionId: string; text: string; truncated: boolean }> {
  const decoder = new TextDecoder();
  let buf = '';
  let sessionId = '';
  let text = '';
  let truncated = false;
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
        let code: string | undefined;
        let raw: string | undefined;
        try {
          const data = JSON.parse(ev.data) as {
            error?: string;
            code?: string;
            detail?: string;
          };
          detail = data.error ?? ev.data;
          if (typeof data.code === 'string') code = data.code;
          if (typeof data.detail === 'string') raw = data.detail;
        } catch {
          /* raw text */
        }
        throw Object.assign(new Error(detail), { code, detail: raw });
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
          const data = JSON.parse(ev.data) as {
            session_id?: string;
            text?: string;
            truncated?: boolean;
          };
          if (data.session_id) sessionId = data.session_id;
          if (data.text) text = data.text;
          if (data.truncated) truncated = true;
        } catch {
          /* ignore */
        }
      }
    }
  }
  return { sessionId, text, truncated };
}

export function defaultProvider(
  providers: string[],
  status: Record<string, boolean>,
): string {
  return providers.find((p) => status[p] !== false) ?? providers[0] ?? '';
}

// The one-tap recovery target: the first allowlisted provider that
// is not the failed one and is not known-unreachable (same
// non-false filter as defaultProvider). Empty when there is no
// live alternative, which hides the switch button.
export function otherProvider(
  provider: string,
  providers: string[],
  status: Record<string, boolean>,
): string {
  return (
    providers.find((p) => p !== provider && status[p] !== false) ?? ''
  );
}

export function GuidanceCard({
  message,
  detail,
  other,
  onSwitch,
}: {
  message: string;
  detail: string;
  other: string;
  onSwitch: () => void;
}) {
  return (
    <div className="detail" data-testid="agent-guidance" role="alert">
      <span className="row-title">Agent failed: {message}</span>
      <div className="actions">
        {other !== '' && (
          <button
            type="button"
            data-testid="agent-switch-provider"
            onClick={onSwitch}
          >
            Switch to {other}
          </button>
        )}
      </div>
      {detail !== '' && (
        <details data-testid="agent-error-detail">
          <summary className="row-meta">Raw output</summary>
          <pre className="row-meta">{detail}</pre>
        </details>
      )}
    </div>
  );
}

export default function AgentDrawer({
  repo,
  initialProviders,
  initialStatus,
}: {
  repo: string;
  initialProviders?: string[];
  initialStatus?: Record<string, boolean>;
}) {
  // Optimistic: the picker renders instantly with the allowlist and
  // live status refines it. Probes are binary checks now (issue
  // #499), so the blind window is milliseconds; a dead provider
  // fails fast into the guidance card.
  const [providers, setProviders] = useState<string[]>(
    initialProviders ?? FALLBACK_PROVIDERS,
  );
  const [status, setStatus] = useState<Record<string, boolean>>(
    initialStatus ?? {},
  );
  const [provider, setProvider] = useState(
    defaultProvider(initialProviders ?? [], initialStatus ?? {}),
  );
  const [prompt, setPrompt] = useState('');
  const [turns, setTurns] = useState<Turn[]>([]);
  const [live, setLive] = useState(false);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [failure, setFailure] = useState<{ code: string; detail: string } | null>(
    null,
  );
  const sidRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (initialProviders) return;
    if (useMock()) {
      setProviders(FALLBACK_PROVIDERS);
      return;
    }
    (async () => {
      try {
        const liveProviders = await fetchAgentProviders();
        setProviders(liveProviders.providers);
        const next: Record<string, boolean> = {};
        for (const s of liveProviders.status ?? []) next[s.name] = s.available;
        setStatus(next);
      } catch {
        // Status is advisory: keep the optimistic picker. A dead
        // provider fails fast into the guidance card on fire.
        setProviders(FALLBACK_PROVIDERS);
      }
    })();
  }, [initialProviders]);

  useEffect(() => {
    if (!provider && providers.length > 0) {
      setProvider(defaultProvider(providers, status));
    }
  }, [provider, providers, status]);

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
      const {
        sessionId,
        text: full,
        truncated,
      } = await pumpTurn(resp.body.getReader(), appendAnswer);
      if (sessionId) sidRef.current = sessionId;
      backfillAnswer(full);
      if (truncated) appendAnswer('\n\n[output truncated — the turn exceeded the size cap]');
    } catch (err) {
      if ((err as Error).name !== 'AbortError') {
        setError(err instanceof Error ? err.message : String(err));
        const code = (err as { code?: unknown }).code;
        const raw = (err as { detail?: unknown }).detail;
        setFailure(
          typeof code === 'string' && code !== ''
            ? { code, detail: typeof raw === 'string' ? raw : '' }
            : null,
        );
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
    setFailure(null);
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
    setFailure(null);
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
    setFailure(null);
    setWorking(false);
    setLive(false);
  }

  function onSwitchProvider() {
    const other = otherProvider(provider, providers, status);
    if (!other) return;
    // Fresh session on the new provider: session ids are
    // provider-side state (agy --continue would resume the wrong
    // conversation), so the sid must not cross over.
    sidRef.current = null;
    setProvider(other);
    setError(null);
    setFailure(null);
  }

  return (
    <section data-testid="agent-drawer">
      <h3>Agent session</h3>
      {error && failure && (
        <GuidanceCard
          message={error}
          detail={failure.detail}
          other={otherProvider(provider, providers, status)}
          onSwitch={onSwitchProvider}
        />
      )}
      {error && !failure && (
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
                  {status[p] === false ? ' (unreachable)' : ''}
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
            <TapToFire
              label="Launch"
              disabled={
                providers.length === 0 ||
                (provider !== '' && status[provider] === false)
              }
              onFire={() => void onLaunch()}
            />
            {provider !== '' && status[provider] === false && (
              <p className="row-meta" data-testid="agent-provider-dead" role="status">
                {provider} is unreachable — pick a live provider.
              </p>
            )}
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
                <p
                  role={
                    t.role === 'assistant' && working && i === turns.length - 1
                      ? 'status'
                      : undefined
                  }
                >
                  {t.text || (working && i === turns.length - 1 ? 'Working…' : '')}
                </p>
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
