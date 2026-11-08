import { describe, expect, it } from 'vitest';
import { parseJson, parseSse, parseSsePart, pumpSse } from './sse';
import { pumpTurn } from './AgentDrawer';
import { pumpStream } from './ChatView';

// Shared-SSE regression (boomtick #1222 item 8): AgentDrawer's
// pumpTurn and ChatView's pumpStream duplicated the same
// split/decode/dispatch loop (incl. five inline JSON.parse catches
// in the drawer). Both now ride the shared pumpSse/parseJson
// helpers from ./sse. These tests prove identical dispatch by
// replaying the exact fragmented sequences the old inline copies
// handled through BOTH the current exports and verbatim
// transcriptions of the removed loops (HEAD
// frontend/src/views/ChatView.tsx parseSse/pumpStream and
// frontend/src/views/AgentDrawer.tsx pumpTurn), then comparing
// callback traces.

// --- Verbatim transcriptions of the removed inline copies. ---

interface LegacyEvent {
  name: string;
  data: string;
}

function legacyParseSsePart(part: string): LegacyEvent | null {
  const name = (part.match(/^event:\s*(.*)$/m)?.[1] ?? '').trim();
  const dataMatch = part.match(/^data:\s*(.*)$/m);
  if (!name || !dataMatch) return null;
  return { name, data: dataMatch[1] };
}

function legacyParseSse(buffer: string): { events: LegacyEvent[]; rest: string } {
  const parts = buffer.split('\n\n');
  const rest = parts.pop() ?? '';
  const events = parts
    .map(legacyParseSsePart)
    .filter((e): e is LegacyEvent => e !== null);
  return { events, rest };
}

function legacyParseJson<T>(data: string): T | null {
  try {
    return JSON.parse(data) as T;
  } catch {
    return null;
  }
}

interface ChatTrace {
  tokens: string[];
  approvals: unknown[];
  phases: Array<string | null>;
  dones: string[];
  events: number;
  error: string | null;
}

function freshChatTrace(): ChatTrace {
  return { tokens: [], approvals: [], phases: [], dones: [], events: 0, error: null };
}

// Old ChatView pumpStream loop, reader replaced by a chunk array;
// callback order, the onEvent()-per-event tick, and the exact
// throw/fallback expressions are unchanged.
async function legacyChatPump(chunks: Uint8Array[], t: ChatTrace): Promise<void> {
  const decoder = new TextDecoder();
  let buf = '';
  try {
    for (const value of chunks) {
      buf += decoder.decode(value, { stream: true });
      const { events, rest } = legacyParseSse(buf);
      buf = rest;
      for (const ev of events) {
        t.events += 1;
        if (ev.name === 'error') {
          const message = legacyParseJson<{ error?: string }>(ev.data)?.error;
          throw new Error(message && message.length > 0 ? message : ev.data);
        }
        if (ev.name === 'token') {
          t.tokens.push(legacyParseJson<{ token?: string }>(ev.data)?.token ?? '');
        }
        if (ev.name === 'approval') {
          const payload = legacyParseJson<unknown>(ev.data);
          if (payload) t.approvals.push(payload);
        }
        if (ev.name === 'phase') {
          const msg = legacyParseJson<{ message?: string }>(ev.data)?.message;
          t.phases.push(msg && msg.length > 0 ? msg : null);
        }
        if (ev.name === 'done') {
          t.phases.push(null);
          t.dones.push(legacyParseJson<{ text?: string }>(ev.data)?.text ?? '');
        }
      }
    }
  } catch (err) {
    t.error = (err as Error).message;
  }
}

async function newChatPump(chunks: Uint8Array[], t: ChatTrace): Promise<void> {
  try {
    await pumpStream(
      chunkReader(chunks),
      (token) => t.tokens.push(token),
      (payload) => t.approvals.push(payload),
      (message) => t.phases.push(message),
      (text) => t.dones.push(text),
      () => {
        t.events += 1;
      },
    );
  } catch (err) {
    t.error = (err as Error).message;
  }
}

interface SalvageTrace {
  branch: string | null;
  commit: string | null;
  committed: boolean;
  pushed: boolean;
}

interface AgentTrace {
  tokens: string[];
  warnings: number[];
  progress: string[];
  result: { sessionId: string; text: string; truncated: boolean } | null;
  error: {
    message: string;
    code?: string;
    detail?: string;
    sessionId?: string;
    salvage?: SalvageTrace;
  } | null;
}

function freshAgentTrace(): AgentTrace {
  return { tokens: [], warnings: [], progress: [], result: null, error: null };
}

// Old AgentDrawer pumpTurn loop, reader replaced by a chunk array;
// all five inline JSON.parse catches keep their exact skip/extract
// semantics.
async function legacyAgentPump(chunks: Uint8Array[], t: AgentTrace): Promise<void> {
  const decoder = new TextDecoder();
  let buf = '';
  let sessionId = '';
  let text = '';
  let truncated = false;
  try {
    for (const value of chunks) {
      buf += decoder.decode(value, { stream: true });
      const parts = buf.split('\n\n');
      buf = parts.pop() ?? '';
      for (const part of parts) {
        const ev = legacyParseSsePart(part);
        if (!ev) continue;
        if (ev.name === 'error') {
          let detail = ev.data;
          let code: string | undefined;
          let raw: string | undefined;
          let errorSessionId: string | undefined;
          let salvage: SalvageTrace | undefined;
          try {
            const data = JSON.parse(ev.data) as {
              error?: string;
              code?: string;
              detail?: string;
              session_id?: string;
              salvage?: SalvageTrace;
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
            if (typeof secs === 'number') t.warnings.push(secs);
          } catch {
            /* ignore */
          }
        }
        if (ev.name === 'progress') {
          try {
            const progress = (JSON.parse(ev.data) as { text?: unknown }).text;
            if (typeof progress === 'string' && progress) t.progress.push(progress);
          } catch {
            /* ignore */
          }
        }
        if (ev.name === 'token') {
          try {
            t.tokens.push((JSON.parse(ev.data) as { token?: string }).token ?? '');
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
    t.result = { sessionId, text, truncated };
  } catch (err) {
    const e = err as Error & {
      code?: string;
      detail?: string;
      sessionId?: string;
      salvage?: SalvageTrace;
    };
    t.error = {
      message: e.message,
      code: e.code,
      detail: e.detail,
      sessionId: e.sessionId,
      salvage: e.salvage,
    };
  }
}

async function newAgentPump(chunks: Uint8Array[], t: AgentTrace): Promise<void> {
  try {
    t.result = await pumpTurn(
      chunkReader(chunks),
      (token) => t.tokens.push(token),
      (secs) => t.warnings.push(secs),
      (progress) => t.progress.push(progress),
    );
  } catch (err) {
    const e = err as Error & {
      code?: string;
      detail?: string;
      sessionId?: string;
      salvage?: SalvageTrace;
    };
    t.error = {
      message: e.message,
      code: e.code,
      detail: e.detail,
      sessionId: e.sessionId,
      salvage: e.salvage,
    };
  }
}

// --- Chunk plumbing (ChatView.test.tsx sseReader shape, byte-level). ---

function chunkReader(chunks: Uint8Array[]): ReadableStreamDefaultReader<Uint8Array> {
  const queue = [...chunks];
  return {
    read: async () => {
      const value = queue.shift();
      if (!value) return { done: true, value: undefined };
      return { done: false, value };
    },
  } as unknown as ReadableStreamDefaultReader<Uint8Array>;
}

const enc = (s: string): Uint8Array => new TextEncoder().encode(s);

// Cut one frame's bytes at `at` so a multibyte character or a JSON
// token straddles the chunk boundary, exactly as TCP delivers it.
function splitBytes(s: string, at: number): Uint8Array[] {
  const bytes = enc(s);
  return [bytes.slice(0, at), bytes.slice(at)];
}

const TOKEN_HELLO = 'event: token\ndata: {"token":"hello"}\n\n';
const DONE_FINAL = 'event: done\ndata: {"text":"final"}\n\n';
const AGENT_DONE =
  'event: done\ndata: {"session_id":"s1","text":"hello","truncated":true}\n\n';
const FULL_ERROR =
  'event: error\ndata: {"error":"muse access expired","code":"auth-expired",' +
  '"detail":"token expired","session_id":"s9","salvage":{"branch":"b",' +
  '"commit":"c","committed":true,"pushed":false}}\n\n';

// Byte offset landing inside the 👋 (4-byte) sequence of the token.
function emojiSplit(): Uint8Array[] {
  const prefix = 'event: token\ndata: {"token":"hi ';
  const at = enc(prefix).length + 2;
  return splitBytes(`${prefix}👋 bye"}\n\n`, at);
}

describe('shared SSE splitter (boomtick #1222 item 8)', () => {
  it('matches the legacy splitter on whole, split, and garbage frames', () => {
    const buffers = [
      TOKEN_HELLO,
      TOKEN_HELLO.slice(0, 20),
      `${TOKEN_HELLO}${DONE_FINAL}`,
      'event: token\ndata: {"token":"a"}\n',
      'garbage with no event line\n\n',
      'event: noname-but-data\ndata: {}\n\n'.replace('event: noname-but-data', 'oops'),
      'event: token\nno-data-here\n\n',
    ];
    for (const buffer of buffers) {
      expect(parseSse(buffer)).toEqual(legacyParseSse(buffer));
    }
  });

  it('keeps parseSsePart edge cases: blank name or missing data is null', () => {
    expect(parseSsePart('event: token\ndata: {"a":1}')).toEqual({
      name: 'token',
      data: '{"a":1}',
    });
    expect(parseSsePart('data: {"a":1}')).toBeNull();
    expect(parseSsePart('event: token')).toBeNull();
    // Quirk pin: the shared name pattern's \s* eats the newline, so
    // a blank event line absorbs the next line as the name. Both
    // removed inline copies had the identical pattern, so parity
    // means keeping this wart, not fixing it here.
    expect(parseSsePart('event: \ndata: x')).toEqual({ name: 'data: x', data: 'x' });
  });

  it('parseJson returns null instead of throwing on partial frames', () => {
    expect(parseJson<{ token?: string }>('{"token":"hel')).toBeNull();
    expect(parseJson<{ token?: string }>('{"token":"hi"}')).toEqual({ token: 'hi' });
  });

  it('pumpSse delivers one event per callback in order and holds a trailing partial', async () => {
    const seen: Array<{ name: string; data: string }> = [];
    await pumpSse(chunkReader([enc(`${TOKEN_HELLO}${DONE_FINAL}`)]), (ev) => {
      seen.push({ ...ev });
    });
    expect(seen).toEqual([
      { name: 'token', data: '{"token":"hello"}' },
      { name: 'done', data: '{"text":"final"}' },
    ]);
    const held: unknown[] = [];
    await pumpSse(chunkReader([enc('event: token\ndata: {"token":"a"}\n')]), (ev) => {
      held.push(ev);
    });
    expect(held).toEqual([]);
  });

  it('a throwing onEvent aborts the pump to the caller', async () => {
    await expect(
      pumpSse(chunkReader([enc(TOKEN_HELLO)]), () => {
        throw new Error('stop');
      }),
    ).rejects.toThrow('stop');
  });
});

describe('chat dispatch parity: pumpStream vs the removed inline loop', () => {
  const sequences: Uint8Array[][] = [
    // Whole events, one per chunk.
    [enc(TOKEN_HELLO)],
    // Token JSON split mid-value across chunks.
    splitBytes(TOKEN_HELLO, 30),
    // Multibyte character split mid-codepoint across chunks.
    emojiSplit(),
    // Corrupt-JSON data frame: the old chat path emitted '' (kept).
    [enc('event: token\ndata: {"token":\n\n')],
    // Error split mid-JSON: human message, not the envelope.
    splitBytes('event: error\ndata: {"error":"unknown repo"}\n\n', 25),
    // Raw-text error and empty-error envelope fall back to raw data.
    [enc('event: error\ndata: boom\n\n')],
    [enc('event: error\ndata: {}\n\n')],
    // Mixed stream split inside the \n\n delimiter and mid-event.
    splitBytes(
      'event: phase\ndata: {"message":"build"}\n\nevent: approval\ndata: {"kind":"x"}\n\n' +
        'event: token\ndata: {"token":"t"}\n\nevent: done\ndata: {"text":"z"}\n\n',
      44,
    ),
    // Garbage parts dropped, order kept across one fat chunk.
    [enc(`oops\n\n${TOKEN_HELLO}event: phase\ndata: {}\n\n${DONE_FINAL}`)],
    // Trailing partial frame held: nothing dispatched.
    [enc('event: token\ndata: {"token":"a"}\n')],
  ];

  it.each(sequences.map((s, i) => [i, s] as [number, Uint8Array[]]))(
    'sequence %i dispatches identically',
    async (_i, chunks) => {
      const legacy = freshChatTrace();
      const current = freshChatTrace();
      await legacyChatPump(chunks, legacy);
      await newChatPump(chunks, current);
      expect(current).toEqual(legacy);
    },
  );

  it('decodes a codepoint split across chunks to the same string', async () => {
    for (const run of [legacyChatPump, newChatPump]) {
      const t = freshChatTrace();
      await run(emojiSplit(), t);
      expect(t.tokens).toEqual(['hi 👋 bye']);
    }
  });
});

describe('agent dispatch parity: pumpTurn vs the removed inline loop', () => {
  const sequences: Uint8Array[][] = [
    // Whole token + done pair.
    [enc('event: token\ndata: {"token":"a"}\n\n'), enc(AGENT_DONE)],
    // Token JSON split mid-value: buffered, then dispatched once whole.
    splitBytes('event: token\ndata: {"token":"ab"}\n\n', 28),
    // Corrupt-JSON token frame: the old drawer path skipped it (no
    // empty-string emit, unlike the chat path above).
    [enc('event: token\ndata: {"token":\n\n'), enc(AGENT_DONE)],
    // Warning + progress, one split mid-number.
    splitBytes(
      'event: warning\ndata: {"seconds_left": 20}\n\nevent: progress\ndata: {"text":"tool"}\n\n',
      35,
    ),
    // Full error envelope split mid-JSON: same message + attachments.
    splitBytes(FULL_ERROR, 40),
    // Raw-text error: message falls back to the raw data.
    [enc('event: error\ndata: boom\n\n')],
    // Trailing partial: resolves empty, dispatches nothing.
    [enc('event: token\ndata: {"token":"a"}\n')],
  ];

  it.each(sequences.map((s, i) => [i, s] as [number, Uint8Array[]]))(
    'sequence %i dispatches identically',
    async (_i, chunks) => {
      const legacy = freshAgentTrace();
      const current = freshAgentTrace();
      await legacyAgentPump(chunks, legacy);
      await newAgentPump(chunks, current);
      expect(current).toEqual(legacy);
    },
  );

  it('keeps the drawer/token distinction: corrupt token JSON is skipped, not emitted', async () => {
    const t = freshAgentTrace();
    await newAgentPump([enc('event: token\ndata: {"token":\n\n')], t);
    expect(t.tokens).toEqual([]);
  });

  it('keeps the enriched error attachments (code, detail, sessionId, salvage)', async () => {
    const t = freshAgentTrace();
    await newAgentPump([enc(FULL_ERROR)], t);
    expect(t.error?.message).toBe('muse access expired');
    expect(t.error?.code).toBe('auth-expired');
    expect(t.error?.detail).toBe('token expired');
    expect(t.error?.sessionId).toBe('s9');
    expect(t.error?.salvage).toEqual({
      branch: 'b',
      commit: 'c',
      committed: true,
      pushed: false,
    });
  });
});
