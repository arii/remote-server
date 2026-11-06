import { useEffect, useRef, useState } from 'react';
import TapToFire from '../components/TapToFire';
import {
  abortAgentSession,
  fetchAgentProviders,
  fetchAgentSession,
  postAgentTurn,
} from '../apiClient';
import type { AgentSessionFinished } from '../apiClient';
import {
  clearAgentSession,
  loadAgentSession,
  saveAgentSession,
  settledAgentTurns,
  transcriptDelta,
} from './agentSessionStore';
import type { AgentTurn } from './agentSessionStore';
import { useMock } from './mockViews';

// Mirrors backend PROVIDERS (agent_turn.py): the picker renders
// these instantly and live status refines them.
const FALLBACK_PROVIDERS = ['muse', 'agy'];

// Reattach poll cadence (issue #856): a resumed live turn's text
// refreshes this often until the server reports it finished.
const REATTACH_POLL_MS = 2000;

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

// Salvage summary from the bounded timeout rescue (issue #838):
// which branch holds the wip snapshot, its commit, and whether
// the push landed.
export interface SalvageInfo {
  branch: string | null;
  commit: string | null;
  committed: boolean;
  pushed: boolean;
}

export function formatElapsed(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  const m = Math.floor(s / 60);
  return `${String(m).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
}

// Pump one turn's SSE stream: token chunks append to the live answer,
// warning reports seconds left via onWarning, done resolves
// {session id, full text, truncated}, error rejects with code,
// detail, sessionId, and salvage attached when the server sent them.
export async function pumpTurn(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  onToken: (token: string) => void,
  onWarning?: (secondsLeft: number) => void,
  onProgress?: (text: string) => void,
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
        let errorSessionId: string | undefined;
        let salvage: SalvageInfo | undefined;
        try {
          const data = JSON.parse(ev.data) as {
            error?: string;
            code?: string;
            detail?: string;
            session_id?: string;
            salvage?: SalvageInfo;
          };
          detail = data.error ?? ev.data;
          if (typeof data.code === 'string') code = data.code;
          if (typeof data.detail === 'string') raw = data.detail;
          if (typeof data.session_id === 'string' && data.session_id !== '') {
            errorSessionId = data.session_id;
          }
          if (data.salvage !== null && typeof data.salvage === 'object') {
            salvage = data.salvage;
          }
        } catch {
          /* raw text */
        }
        throw Object.assign(new Error(detail), {
          code,
          detail: raw,
          sessionId: errorSessionId,
          salvage,
        });
      }
      if (ev.name === 'warning') {
        try {
          const secs = (JSON.parse(ev.data) as { seconds_left?: unknown })
            .seconds_left;
          if (typeof secs === 'number') onWarning?.(secs);
        } catch {
          /* ignore */
        }
      }
      // Live progress (issue #1021): tool calls and phase changes
      // land here, never in the transcript tokens.
      if (ev.name === 'progress') {
        try {
          const text = (JSON.parse(ev.data) as { text?: unknown }).text;
          if (typeof text === 'string' && text) onProgress?.(text);
        } catch {
          /* ignore */
        }
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

function defaultProvider(
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

// Launch guard (issue #1037): onLaunch must never no-op. A Launch
// click with no provider, or against a stale live flag, used to
// return silently — no Working, no error, no Abort. Either case
// now names the block so the operator can act on it.
export function launchBlockReason(provider: string, live: boolean): string | null {
  if (!provider)
    return 'No agent provider is available — check the terminal CLIs.';
  if (live)
    return 'A session is already live — use Send, or New session to start over.';
  return null;
}

export function GuidanceCard({
  message,
  detail,
  other,
  onSwitch,
  salvage,
}: {
  message: string;
  detail: string;
  other: string;
  onSwitch: () => void;
  salvage?: SalvageInfo | null;
}) {
  return (
    <div className="detail" data-testid="agent-guidance" role="alert">
      <span className="row-title">Agent failed: {message}</span>
      {salvage !== null && salvage !== undefined && (
        <div className="row-meta" data-testid="agent-salvage">
          <span>Branch: {salvage.branch ?? 'unknown'}</span>{' '}
          <span>
            Commit:{' '}
            {salvage.commit !== null
              ? salvage.commit.slice(0, 12)
              : 'nothing committed'}
          </span>{' '}
          <span>Pushed: {salvage.pushed ? 'yes' : 'no'}</span>
        </div>
      )}
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
  // Restored live session (issue #856): the localStorage snapshot
  // replays so a reload keeps the transcript instead of wiping to
  // the launch form. A snapshot belongs to the repo it was taken
  // in — another repo's drawer starts fresh. Mock mode starts
  // empty and never touches the store.
  const [restored] = useState(() =>
    useMock() ? null : loadAgentSession(),
  );
  const snap =
    restored !== null && restored.repo === repo ? restored : null;
  const [providers, setProviders] = useState<string[]>(
    initialProviders ?? FALLBACK_PROVIDERS,
  );
  const [status, setStatus] = useState<Record<string, boolean>>(
    initialStatus ?? {},
  );
  const [provider, setProvider] = useState(
    snap?.provider || defaultProvider(initialProviders ?? [], initialStatus ?? {}),
  );
  const [prompt, setPrompt] = useState('');
  const [turns, setTurns] = useState<AgentTurn[]>(snap?.turns ?? []);
  const [live, setLive] = useState(snap?.live ?? false);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [failure, setFailure] = useState<{ code: string; detail: string } | null>(
    null,
  );
  // Turn survivability (issue #838): lastPrompt replays the failed
  // turn on one-tap Resume; salvage reports the timeout rescue;
  // warningSecs counts down the server's pre-timeout warning; now
  // ticks the elapsed/stall clock while a turn is working.
  const [lastPrompt, setLastPrompt] = useState(snap?.lastPrompt ?? '');
  const [salvage, setSalvage] = useState<SalvageInfo | null>(null);
  const [warningSecs, setWarningSecs] = useState<number | null>(null);
  // Live progress lines (issue #1021): tool calls and phase
  // changes, newest last, capped — rendered collapsed, never
  // persisted with the transcript snapshot.
  const [progress, setProgress] = useState<string[]>([]);
  const [now, setNow] = useState(0);
  // Reload reattach (issue #856): orphan marks a restored session
  // the server no longer knows (Resume reclaims it); reattachNote
  // reports interrupted/offline restores. reattachRef generations
  // the mount reattach run so abort/new/unmount retires its poll.
  const [orphan, setOrphan] = useState(false);
  const [reattachNote, setReattachNote] = useState<string | null>(null);
  const sidRef = useRef<string | null>(snap?.sessionId ?? null);
  const abortRef = useRef<AbortController | null>(null);
  const startRef = useRef(0);
  const tokenRef = useRef(0);
  const reattachRef = useRef(0);

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

  useEffect(() => {
    if (!working) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [working]);

  // Persist the live session across reloads (issue #856): every
  // transcript change lands in localStorage for the initializer
  // above. `working` is a dep so the session id (a ref, set when a
  // turn completes) persists with its transcript. Pristine state
  // removes the snapshot instead of writing an empty one.
  useEffect(() => {
    if (useMock()) return;
    if (!live && turns.length === 0 && sidRef.current === null) {
      clearAgentSession();
      return;
    }
    saveAgentSession({
      sessionId: sidRef.current,
      provider,
      repo,
      turns: settledAgentTurns(turns),
      lastPrompt,
      live,
    });
  }, [turns, live, lastPrompt, provider, repo, working]);

  // Reattach after reload (issue #856): a restored live session asks
  // the server for its state instead of firing a duplicate turn. A
  // still-running turn resumes its text by polling; a finished turn
  // backfills its verdict; an unknown id degrades to a readable
  // orphan the operator can Resume (reclaim) or abandon. Mount-only:
  // later turns stream over their own POST, never this poll.
  useEffect(() => {
    if (useMock()) return;
    if (!snap || !snap.live) return;
    const sid = sidRef.current;
    if (sid === null) {
      // Reloaded before the done event returned an id: the server
      // turn (if any) is unreachable without it, so the transcript
      // stays and Resume fires a fresh turn.
      if (snap.turns.length > 0) {
        setReattachNote('Turn was interrupted by the reload — Resume re-fires it.');
      }
      // Stale-live reset (issue #1037): after a reload nothing
      // streams, so a live flag with no session id is always dead —
      // without this the launch form stays hidden with no Working,
      // no error, and no Abort. The transcript and note survive.
      setLive(false);
      return;
    }
    const run = reattachRef.current + 1;
    reattachRef.current = run;
    const alive = () => reattachRef.current === run;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const schedule = () => {
      if (alive()) timer = setTimeout(() => void poll(), REATTACH_POLL_MS);
    };
    const poll = async (): Promise<void> => {
      if (!alive()) return;
      let next;
      try {
        next = await fetchAgentSession(sid);
      } catch {
        schedule();
        return;
      }
      if (!alive()) return;
      applyServerTranscript(next.transcript);
      tokenRef.current = Date.now();
      setNow(Date.now());
      if (!next.running) {
        setWorking(false);
        if (next.finished) {
          finishReattachedTurn(next.finished, next.transcript_truncated);
        } else if (alive()) {
          setReattachNote('Turn was interrupted — Resume re-fires it.');
        }
        return;
      }
      schedule();
    };
    (async () => {
      let state;
      try {
        state = await fetchAgentSession(sid);
      } catch {
        if (alive()) {
          setReattachNote(
            "Couldn't reach the server — showing the saved transcript.",
          );
        }
        return;
      }
      if (!alive()) return;
      if (!state.known) {
        setOrphan(true);
        return;
      }
      if (!state.running && !state.finished) {
        // Known but idle with no verdict: the stream died with the
        // reload before the turn completed.
        setReattachNote('Turn was interrupted by the reload — Resume re-fires it.');
        return;
      }
      applyServerTranscript(state.transcript);
      if (!state.running && state.finished) {
        finishReattachedTurn(state.finished, state.transcript_truncated);
        return;
      }
      const at = Date.now();
      startRef.current = at;
      tokenRef.current = at;
      setNow(at);
      setWorking(true);
      schedule();
    })();
    return () => {
      reattachRef.current += 1;
      if (timer !== null) clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const streamedRef = useRef(0);

  function appendAnswer(token: string) {
    if (!token) return;
    streamedRef.current += token.length;
    tokenRef.current = Date.now();
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

  function dropEmptyBubble() {
    // A failed turn leaves an empty assistant bubble behind; retry
    // sends cleanly only once it is gone.
    setTurns((t) => {
      const last = t[t.length - 1];
      if (last && last.role === 'assistant' && !last.text) return t.slice(0, -1);
      return t;
    });
  }

  // Fold the server's live buffer into the restored trailing answer
  // (issue #856): while the same turn streams, only the unseen
  // suffix appends. The settled snapshot trimmed the live typing
  // placeholder, so a user-final transcript opens the bubble first.
  function applyServerTranscript(server: string) {
    setTurns((t) => {
      const last = t[t.length - 1];
      if (!last || last.role !== 'assistant') {
        if (!server) return t;
        return [...t, { role: 'assistant', text: server }];
      }
      const delta = transcriptDelta(last.text, server);
      if (delta.mode === 'append') {
        if (!delta.text) return t;
        const next = [...t];
        next[next.length - 1] = {
          role: 'assistant',
          text: last.text + delta.text,
        };
        return next;
      }
      const next = [...t];
      next[next.length - 1] = { role: 'assistant', text: delta.text };
      return next;
    });
  }

  // Verdict for a turn that finished while the console was gone
  // (issue #856): mirrors sendTurn's done/error handling so a
  // reattached finish renders exactly like a live one.
  function finishReattachedTurn(
    fin: AgentSessionFinished,
    liveTruncated: boolean,
  ) {
    if (fin.truncated || liveTruncated) {
      appendAnswer('\n\n[output truncated — the turn exceeded the size cap]');
    }
    if (fin.timed_out) {
      const minutes = Math.max(1, Math.floor(fin.timeout / 60));
      setError(`turn timed out after ${minutes}m`);
      setFailure({ code: 'turn-timeout', detail: '' });
      setSalvage(fin.rescue ?? null);
      dropEmptyBubble();
      return;
    }
    if (fin.exit !== 0) {
      if (fin.guidance_code) {
        setError(fin.guidance_message || 'Agent turn failed');
        setFailure({
          code: fin.guidance_code,
          detail: fin.guidance_detail || '',
        });
      } else {
        const lines = fin.stderr.trim().split('\n');
        const tail = lines[lines.length - 1] || `exit ${fin.exit ?? -1}`;
        setError(tail.slice(0, 500));
        setFailure(null);
      }
      dropEmptyBubble();
      return;
    }
  }

  async function sendTurn(text: string, reclaim = false) {
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    // A fresh turn supersedes any mount reattach poll still in flight.
    reattachRef.current += 1;
    const at = Date.now();
    startRef.current = at;
    tokenRef.current = at;
    setNow(at);
    setWarningSecs(null);
    setProgress([]);
    setSalvage(null);
    setOrphan(false);
    setReattachNote(null);
    setLastPrompt(text);
    setWorking(true);
    setTurns((t) => [...t, { role: 'assistant', text: '' }]);
    try {
      if (useMock() && !initialProviders) {
        appendAnswer('Mock agent reply.');
        return;
      }
      const resp = await postAgentTurn(
        provider,
        repo,
        text,
        sidRef.current,
        ctrl.signal,
        reclaim,
      );
      if (!resp.ok || !resp.body) throw new Error(`Agent turn failed: ${resp.status}`);
      streamedRef.current = 0;
      const {
        sessionId,
        text: full,
        truncated,
      } = await pumpTurn(
        resp.body.getReader(),
        appendAnswer,
        (secs) => setWarningSecs(secs),
        (line) => setProgress((p) => [...p.slice(-49), line]),
      );
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
        // Timeout errors carry the session so Resume continues the
        // same server-known session, plus the rescue's salvage.
        const errSid = (err as { sessionId?: unknown }).sessionId;
        if (typeof errSid === 'string' && errSid !== '' && !sidRef.current) {
          sidRef.current = errSid;
        }
        const errSalvage = (err as { salvage?: unknown }).salvage;
        setSalvage(
          errSalvage !== null && typeof errSalvage === 'object'
            ? (errSalvage as SalvageInfo)
            : null,
        );
        dropEmptyBubble();
      }
    } finally {
      abortRef.current = null;
      setWorking(false);
    }
  }

  async function onLaunch() {
    const blocked = launchBlockReason(provider, live);
    if (blocked) {
      setError(blocked);
      return;
    }
    const text = prompt.trim();
    if (!text) {
      setError('Describe the task first.');
      return;
    }
    setError(null);
    setFailure(null);
    setSalvage(null);
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
    setSalvage(null);
    setTurns((t) => [...t, { role: 'user', text }]);
    setPrompt('');
    await sendTurn(text);
  }

  // One-tap Resume (issue #838): replay the failed turn's prompt on
  // the same session — the user bubble is already in the transcript,
  // so only the assistant turn re-fires. From an orphan (issue #856)
  // the turn reclaims the id with a spawn confirm, since the server
  // no longer knows it.
  async function onResume() {
    if (working || lastPrompt === '') return;
    setError(null);
    setFailure(null);
    setSalvage(null);
    await sendTurn(lastPrompt, orphan);
  }

  async function onAbort() {
    abortRef.current?.abort();
    // Retire the reattach poll: its verdicts belong to the killed turn.
    reattachRef.current += 1;
    const sid = sidRef.current;
    setWorking(false);
    // Abort kills server-side and clears the persisted snapshot; the
    // settled transcript re-persists via the effect as history.
    clearAgentSession();
    if (!sid || (useMock() && !initialProviders)) return;
    try {
      await abortAgentSession(sid);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  function onNewSession() {
    abortRef.current?.abort();
    reattachRef.current += 1;
    sidRef.current = null;
    setTurns([]);
    setError(null);
    setFailure(null);
    setSalvage(null);
    setWarningSecs(null);
    setLastPrompt('');
    setWorking(false);
    setLive(false);
    setOrphan(false);
    setReattachNote(null);
    clearAgentSession();
  }

  function onSwitchProvider() {
    const other = otherProvider(provider, providers, status);
    if (!other) return;
    // Fresh session on the new provider: session ids are
    // provider-side state (each provider pins its own conversation),
    // so the sid must not cross over. Any orphan/reattach state
    // belonged to the dropped id.
    sidRef.current = null;
    setProvider(other);
    setError(null);
    setFailure(null);
    setSalvage(null);
    setOrphan(false);
    setReattachNote(null);
  }

  return (
    <section data-testid="agent-drawer" className="chat-module">
      <h3>Agent session</h3>
      {error && failure && (
        <GuidanceCard
          message={error}
          detail={failure.detail}
          other={otherProvider(provider, providers, status)}
          onSwitch={onSwitchProvider}
          salvage={salvage}
        />
      )}
      {error && !failure && (
        <p className="row-meta" data-testid="agent-error" role="alert">
          Agent failed: {error}
        </p>
      )}
      {orphan && (
        <p className="row-meta" data-testid="agent-orphan" role="status">
          Session ended while away — transcript kept. Resume reclaims it,
          or start a new session.
        </p>
      )}
      {reattachNote !== null && !orphan && (
        <p className="row-meta" data-testid="agent-reattach-note" role="status">
          {reattachNote}
        </p>
      )}
      {(error !== null || orphan || reattachNote !== null) && !working && lastPrompt !== '' && (
        <div className="actions">
          <button
            type="button"
            data-testid="agent-resume"
            onClick={() => void onResume()}
          >
            Resume
          </button>
        </div>
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
          {working && warningSecs !== null && (
            <p
              className="row-meta"
              data-testid="agent-timeout-warning"
              role="status"
            >
              Turn ends in ~{Math.max(1, Math.round(warningSecs / 60))} min —
              partial work will be committed and pushed.
            </p>
          )}
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
                {working && t.role === 'assistant' && i === turns.length - 1 && (
                  <p className="row-meta" data-testid="agent-elapsed">
                    {formatElapsed((now - startRef.current) / 1000)} · idle{' '}
                    {formatElapsed((now - tokenRef.current) / 1000)}
                  </p>
                )}
                {progress.length > 0 &&
                  t.role === 'assistant' &&
                  i === turns.length - 1 && (
                    <details data-testid="agent-progress">
                      <summary className="row-meta">
                        Working details ({progress.length})
                      </summary>
                      <ul className="agent-progress-list">
                        {progress.map((line, j) => (
                          // eslint-disable-next-line react/no-array-index-key
                          <li key={j} className="row-meta">
                            {line}
                          </li>
                        ))}
                      </ul>
                    </details>
                  )}
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
