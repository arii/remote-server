import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import AgentDrawer, {
  GuidanceCard,
  otherProvider,
  pumpTurn,
} from './AgentDrawer';

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
