import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  AGENT_SESSION_KEY,
  clearAgentSession,
  loadAgentSession,
  saveAgentSession,
  settledAgentTurns,
  transcriptDelta,
} from './agentSessionStore';
import type { AgentTurn } from './agentSessionStore';

function stubLocalStorage(seed: Record<string, string> = {}) {
  const store = new Map<string, string>(Object.entries(seed));
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => (store.has(k) ? (store.get(k) as string) : null),
    setItem: (k: string, v: string) => {
      store.set(k, v);
    },
    removeItem: (k: string) => {
      store.delete(k);
    },
  });
  return store;
}

describe('agent session persistence (issue #856)', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('round-trips the session through localStorage under the agent key', () => {
    const store = stubLocalStorage();
    const turns: AgentTurn[] = [
      { role: 'user', text: 'ship it' },
      { role: 'assistant', text: 'done' },
    ];
    saveAgentSession({
      sessionId: 'sid-1',
      provider: 'muse',
      repo: 'remote-server',
      turns,
      lastPrompt: 'ship it',
      live: true,
    });
    const raw = store.get(AGENT_SESSION_KEY);
    expect(raw).toBeDefined();
    expect(JSON.parse(raw as string).turns).toEqual(turns);
    expect(loadAgentSession()).toMatchObject({
      version: 1,
      sessionId: 'sid-1',
      provider: 'muse',
      repo: 'remote-server',
      turns,
      lastPrompt: 'ship it',
      live: true,
    });
  });

  it('returns null on a corrupt, misshapen, or versioned snapshot', () => {
    stubLocalStorage({ [AGENT_SESSION_KEY]: 'not-json{{' });
    expect(loadAgentSession()).toBeNull();
    stubLocalStorage({
      [AGENT_SESSION_KEY]: JSON.stringify({ version: 1, live: true }),
    });
    expect(loadAgentSession()).toBeNull();
    stubLocalStorage({
      [AGENT_SESSION_KEY]: JSON.stringify({
        version: 2,
        sessionId: null,
        provider: 'muse',
        repo: 'r',
        turns: [],
        lastPrompt: '',
        live: false,
        updatedAt: 0,
      }),
    });
    expect(loadAgentSession()).toBeNull();
    stubLocalStorage({
      [AGENT_SESSION_KEY]: JSON.stringify({
        version: 1,
        sessionId: null,
        provider: 'muse',
        repo: 'r',
        turns: [{ role: 'system', text: 'x' }],
        lastPrompt: '',
        live: false,
        updatedAt: 0,
      }),
    });
    expect(loadAgentSession()).toBeNull();
  });

  it('treats missing localStorage as empty without throwing', () => {
    vi.stubGlobal('localStorage', undefined);
    expect(loadAgentSession()).toBeNull();
    expect(() =>
      saveAgentSession({
        sessionId: null,
        provider: '',
        repo: 'r',
        turns: [],
        lastPrompt: '',
        live: false,
      }),
    ).not.toThrow();
    expect(() => clearAgentSession()).not.toThrow();
  });

  it('clears the stored snapshot', () => {
    const store = stubLocalStorage();
    saveAgentSession({
      sessionId: 'sid-1',
      provider: 'muse',
      repo: 'r',
      turns: [],
      lastPrompt: '',
      live: false,
    });
    expect(store.has(AGENT_SESSION_KEY)).toBe(true);
    clearAgentSession();
    expect(store.has(AGENT_SESSION_KEY)).toBe(false);
    expect(loadAgentSession()).toBeNull();
  });

  it('trims only a trailing empty assistant placeholder', () => {
    expect(
      settledAgentTurns([
        { role: 'user', text: 'hi' },
        { role: 'assistant', text: '' },
      ]),
    ).toEqual([{ role: 'user', text: 'hi' }]);
    const settled: AgentTurn[] = [
      { role: 'user', text: 'hi' },
      { role: 'assistant', text: 'yo' },
    ];
    expect(settledAgentTurns(settled)).toEqual(settled);
    expect(settledAgentTurns([])).toEqual([]);
  });
});

describe('reattach transcript delta (issue #856)', () => {
  it('appends the unseen suffix while the same turn streams', () => {
    expect(transcriptDelta('hel', 'hello')).toEqual({
      mode: 'append',
      text: 'lo',
    });
    expect(transcriptDelta('', 'hello')).toEqual({
      mode: 'append',
      text: 'hello',
    });
  });

  it('appends nothing when the server has no new text', () => {
    expect(transcriptDelta('hello', 'hello')).toEqual({
      mode: 'append',
      text: '',
    });
  });

  it('replaces the bubble when the buffer diverged', () => {
    expect(transcriptDelta('stale text', 'fresh turn')).toEqual({
      mode: 'replace',
      text: 'fresh turn',
    });
  });
});
