import { afterEach, describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import AgentDrawer, {
  GuidanceCard,
  formatElapsed,
  launchBlockReason,
  otherProvider,
  pumpTurn,
} from './AgentDrawer';
import { AGENT_SESSION_KEY } from './agentSessionStore';

function streamOf(text: string): ReadableStreamDefaultReader<Uint8Array> {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(text));
      controller.close();
    },
  });
  return stream.getReader();
}

describe('turn failure detail (issue #410)', () => {
  it('rejects with code and raw detail attached', async () => {
    const reader = streamOf(
      'event: error\n' +
        'data: {"error": "muse access expired", ' +
        '"code": "auth-expired", "detail": "token expired"}\n\n',
    );
    const caught = await pumpTurn(reader, () => {}).then(
      () => null,
      (err: unknown) => err,
    );
    expect(caught).toBeInstanceOf(Error);
    expect((caught as Error).message).toBe('muse access expired');
    expect((caught as { code?: string }).code).toBe('auth-expired');
    expect((caught as { detail?: string }).detail).toBe('token expired');
  });

  it('rejects plain for unstructured errors', async () => {
    const reader = streamOf('event: error\ndata: {"error": "bad"}\n\n');
    const caught = await pumpTurn(reader, () => {}).then(
      () => null,
      (err: unknown) => err,
    );
    expect((caught as Error).message).toBe('bad');
    expect((caught as { code?: string }).code).toBeUndefined();
  });
});

describe('provider switch (issue #410)', () => {
  it('picks the other allowlisted provider', () => {
    expect(otherProvider('muse', ['muse', 'agy'], {})).toBe('agy');
    expect(otherProvider('agy', ['muse', 'agy'], {})).toBe('muse');
  });

  it('returns empty with no alternative', () => {
    expect(otherProvider('muse', ['muse'], {})).toBe('');
    expect(otherProvider('muse', [], {})).toBe('');
  });
});

describe('launch guard (issue #1037)', () => {
  it('names the block instead of no-op silence', () => {
    expect(launchBlockReason('', false)).toContain('No agent provider');
    expect(launchBlockReason('agy', true)).toContain('already live');
  });

  it('clears a ready launch', () => {
    expect(launchBlockReason('agy', false)).toBeNull();
    expect(launchBlockReason('muse', false)).toBeNull();
  });
});

describe('provider switch availability (issue #497)', () => {
  it('returns empty when the only alternative is unreachable', () => {
    expect(
      otherProvider('muse', ['muse', 'agy'], { muse: false, agy: false }),
    ).toBe('');
  });

  it('never returns a status-false target', () => {
    expect(
      otherProvider(
        'muse',
        ['muse', 'agy', 'codex'],
        { muse: false, agy: false, codex: true },
      ),
    ).toBe('codex');
    expect(
      otherProvider('muse', ['muse', 'agy'], { muse: true, agy: false }),
    ).toBe('');
  });

  it('treats unknown status as live like the default pick', () => {
    expect(
      otherProvider('muse', ['muse', 'agy'], { muse: false }),
    ).toBe('agy');
  });
});

describe('guidance card (issue #410)', () => {
  it('shows guidance, switch, and expandable raw tail', () => {
    const html = renderToStaticMarkup(
      <GuidanceCard
        message="muse access expired — sign in again"
        detail="token expired"
        other="agy"
        onSwitch={() => {}}
      />,
    );
    expect(html).toContain('data-testid="agent-guidance"');
    expect(html).toContain('muse access expired');
    expect(html).toContain('data-testid="agent-switch-provider"');
    expect(html).toContain('agy');
    expect(html).toContain('data-testid="agent-error-detail"');
    expect(html).toContain('token expired');
  });

  it('omits the switch without an alternative', () => {
    const html = renderToStaticMarkup(
      <GuidanceCard
        message="muse access expired"
        detail=""
        other=""
        onSwitch={() => {}}
      />,
    );
    expect(html).toContain('data-testid="agent-guidance"');
    expect(html).not.toContain('agent-switch-provider');
  });
});

describe('instant picker (issue #499)', () => {
  it('renders provider options before status loads', () => {
    const html = renderToStaticMarkup(<AgentDrawer repo="remote-server" />);
    expect(html).toContain('data-testid="agent-provider"');
    expect(html).toContain('value="muse"');
    expect(html).toContain('value="agy"');
  });
});

describe('elapsed clock (issue #838)', () => {
  it('formats mm:ss', () => {
    expect(formatElapsed(0)).toBe('00:00');
    expect(formatElapsed(7)).toBe('00:07');
    expect(formatElapsed(75)).toBe('01:15');
    expect(formatElapsed(1799.9)).toBe('29:59');
    expect(formatElapsed(-3)).toBe('00:00');
  });
});

describe('timeout warning (issue #838)', () => {
  it('reports warning events without touching the transcript', async () => {
    const seen: number[] = [];
    const tokens: string[] = [];
    const reader = streamOf(
      'event: token\ndata: {"token": "hi"}\n\n' +
        'event: warning\ndata: {"seconds_left": 120}\n\n' +
        'event: done\ndata: {"session_id": "s1", "text": "hi"}\n\n',
    );
    const done = await pumpTurn(
      reader,
      (t) => tokens.push(t),
      (s) => seen.push(s),
    );
    expect(seen).toEqual([120]);
    expect(tokens).toEqual(['hi']);
    expect(done.text).toBe('hi');
  });

  it('ignores warnings without a callback', async () => {
    const reader = streamOf(
      'event: warning\ndata: {"seconds_left": 5}\n\n' +
        'event: done\ndata: {"session_id": "s1", "text": ""}\n\n',
    );
    const done = await pumpTurn(reader, () => {});
    expect(done.sessionId).toBe('s1');
  });
});

describe('timeout salvage + resume (issue #838)', () => {
  const SALVAGE = {
    branch: 'feat-z',
    commit: 'd'.repeat(40),
    committed: true,
    pushed: false,
  };

  it('attaches session id and salvage to timeout errors', async () => {
    const reader = streamOf(
      'event: error\ndata: {"error": "turn timed out after 30m", ' +
        '"code": "turn-timeout", "session_id": "sid-9", ' +
        '"salvage": {"branch": "feat-z", "commit": "' +
        'd'.repeat(40) +
        '", "committed": true, "pushed": false}}\n\n',
    );
    const caught = await pumpTurn(reader, () => {}).then(
      () => null,
      (err: unknown) => err,
    );
    expect((caught as Error).message).toBe('turn timed out after 30m');
    expect((caught as { code?: string }).code).toBe('turn-timeout');
    expect((caught as { sessionId?: string }).sessionId).toBe('sid-9');
    expect((caught as { salvage?: unknown }).salvage).toEqual(SALVAGE);
  });

  it('leaves session and salvage undefined for plain errors', async () => {
    const reader = streamOf('event: error\ndata: {"error": "bad"}\n\n');
    const caught = await pumpTurn(reader, () => {}).then(
      () => null,
      (err: unknown) => err,
    );
    expect((caught as { sessionId?: string }).sessionId).toBeUndefined();
    expect((caught as { salvage?: unknown }).salvage).toBeUndefined();
  });

  it('renders the salvage summary on the guidance card', () => {
    const html = renderToStaticMarkup(
      <GuidanceCard
        message="turn timed out after 30m"
        detail=""
        other="agy"
        onSwitch={() => {}}
        salvage={SALVAGE}
      />,
    );
    expect(html).toContain('data-testid="agent-salvage"');
    expect(html).toContain('feat-z');
    expect(html).toContain('d'.repeat(12));
    expect(html).toContain('Pushed: no');
  });

  it('omits the salvage block without salvage', () => {
    const html = renderToStaticMarkup(
      <GuidanceCard
        message="muse access expired"
        detail=""
        other=""
        onSwitch={() => {}}
      />,
    );
    expect(html).not.toContain('agent-salvage');
  });
});

describe('session restore on mount (issue #856)', () => {
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

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function snapshot(repo: string) {
    return {
      [AGENT_SESSION_KEY]: JSON.stringify({
        version: 1,
        sessionId: 'sid-9',
        provider: 'agy',
        repo,
        turns: [
          { role: 'user', text: 'ship it' },
          { role: 'assistant', text: 'working' },
        ],
        lastPrompt: 'ship it',
        live: true,
        updatedAt: Date.now(),
      }),
    };
  }

  it('restores the transcript instead of the launch form', () => {
    stubLocalStorage(snapshot('remote-server'));
    const html = renderToStaticMarkup(
      <AgentDrawer repo="remote-server" />,
    );
    expect(html).toContain('data-testid="agent-msg-user-0"');
    expect(html).toContain('ship it');
    expect(html).toContain('working');
    expect(html).toContain('data-testid="agent-followup"');
    expect(html).not.toContain('data-testid="agent-prompt"');
  });

  it('ignores a snapshot from another repo', () => {
    stubLocalStorage(snapshot('other-repo'));
    const html = renderToStaticMarkup(
      <AgentDrawer repo="remote-server" />,
    );
    expect(html).toContain('data-testid="agent-prompt"');
    expect(html).not.toContain('ship it');
  });

  it('starts fresh on a corrupt snapshot', () => {
    stubLocalStorage({ [AGENT_SESSION_KEY]: 'not-json{{' });
    const html = renderToStaticMarkup(
      <AgentDrawer repo="remote-server" />,
    );
    expect(html).toContain('data-testid="agent-prompt"');
  });
});

describe('rail module (issue #897)', () => {
  it('agent drawer renders as a distinct module', () => {
    const html = renderToStaticMarkup(<AgentDrawer repo="remote-server" />);
    expect(html).toContain('class="chat-module"');
  });
});
