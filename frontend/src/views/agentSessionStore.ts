// Reload-proof agent sessions (issue #856): the Agent Session panel
// used to wipe back to the launch form on every console reload,
// orphaning the server-side conversation. The live session now
// persists to localStorage on every transcript change and restores
// on mount, so a reload resumes the same session instead of
// stranding it. Same typeof/try-catch guards as the chat snapshots:
// storage failures only drop persistence, never the live panel.
export interface AgentTurn {
  role: 'user' | 'assistant';
  text: string;
}

export interface AgentSessionSnapshot {
  version: 1;
  sessionId: string | null;
  provider: string;
  repo: string;
  turns: AgentTurn[];
  lastPrompt: string;
  live: boolean;
  updatedAt: number;
}

export const AGENT_SESSION_KEY = 'buttons.agent.session.v1';

function isAgentTurn(value: unknown): value is AgentTurn {
  if (!value || typeof value !== 'object') return false;
  const t = value as Record<string, unknown>;
  return (
    (t.role === 'user' || t.role === 'assistant')
    && typeof t.text === 'string'
  );
}

function isSnapshot(value: unknown): value is AgentSessionSnapshot {
  if (!value || typeof value !== 'object') return false;
  const s = value as Record<string, unknown>;
  return (
    s.version === 1
    && (s.sessionId === null || typeof s.sessionId === 'string')
    && typeof s.provider === 'string'
    && typeof s.repo === 'string'
    && Array.isArray(s.turns)
    && s.turns.every(isAgentTurn)
    && typeof s.lastPrompt === 'string'
    && typeof s.live === 'boolean'
    && typeof s.updatedAt === 'number'
  );
}

export function loadAgentSession(): AgentSessionSnapshot | null {
  try {
    if (typeof localStorage === 'undefined') return null;
    const raw = localStorage.getItem(AGENT_SESSION_KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    return isSnapshot(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

export function saveAgentSession(
  snap: Omit<AgentSessionSnapshot, 'version' | 'updatedAt'>,
): void {
  try {
    if (typeof localStorage === 'undefined') return;
    const full: AgentSessionSnapshot = {
      ...snap,
      version: 1,
      updatedAt: Date.now(),
    };
    localStorage.setItem(AGENT_SESSION_KEY, JSON.stringify(full));
  } catch {
    // Persistence is best-effort; the live session still works.
  }
}

export function clearAgentSession(): void {
  try {
    if (typeof localStorage === 'undefined') return;
    localStorage.removeItem(AGENT_SESSION_KEY);
  } catch {
    // Persistence is best-effort; the live session still works.
  }
}

// Persisted snapshots hold the settled transcript only: a trailing
// empty assistant entry is the live typing placeholder (meaningless
// once streaming stops), so it never reaches localStorage. The live
// state keeps it — only the snapshot is trimmed.
export function settledAgentTurns(turns: AgentTurn[]): AgentTurn[] {
  const last = turns[turns.length - 1];
  if (last && last.role === 'assistant' && last.text === '') {
    return turns.slice(0, -1);
  }
  return turns;
}

export type TranscriptDelta =
  | { mode: 'append'; text: string }
  | { mode: 'replace'; text: string };

// Reconcile the restored trailing answer with the server's live
// buffer: while the same turn streams, the server text extends the
// local prefix, so only the suffix appends. Anything else (a newer
// turn reset the buffer, or the ids never matched) replaces the
// bubble with the server text outright.
export function transcriptDelta(
  known: string,
  server: string,
): TranscriptDelta {
  if (server.startsWith(known)) {
    return { mode: 'append', text: server.slice(known.length) };
  }
  return { mode: 'replace', text: server };
}
