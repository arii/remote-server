// Shared SSE frame helpers: AgentDrawer's pumpTurn and ChatView's
// pumpStream decoded the same event stream with duplicated
// splitters and inline JSON.parse catches. One pump and one JSON
// reader serve both; the views keep only their per-event mapping,
// so stream behavior is unchanged.
export interface SseEvent {
  name: string;
  data: string;
}

export function parseSsePart(part: string): SseEvent | null {
  const name = (part.match(/^event:\s*(.*)$/m)?.[1] ?? '').trim();
  const dataMatch = part.match(/^data:\s*(.*)$/m);
  if (!name || !dataMatch) return null;
  return { name, data: dataMatch[1] };
}

export function parseSse(buffer: string): { events: SseEvent[]; rest: string } {
  const parts = buffer.split('\n\n');
  const rest = parts.pop() ?? '';
  const events = parts
    .map(parseSsePart)
    .filter((e): e is SseEvent => e !== null);
  return { events, rest };
}

export function parseJson<T>(data: string): T | null {
  try {
    return JSON.parse(data) as T;
  } catch {
    return null;
  }
}

// Additive frame id (issue #1208): the backend stamps every SSE
// JSON frame with a process-wide monotonic `seq`. No-seq frames
// (old servers, heartbeats) return null and always pass.
function sseSeq(ev: SseEvent): number | null {
  const seq = parseJson<{ seq?: unknown }>(ev.data)?.seq;
  return typeof seq === 'number' && Number.isFinite(seq) ? seq : null;
}

export interface SseReconciler {
  fresh(ev: SseEvent): boolean;
  last(): number | null;
  reset(): void;
}

// Drop stale/redelivered frames after a socket drop + reconnect:
// a frame whose seq is at or behind the last applied one was
// already rendered. The seen window is bounded (default 200): only
// the high-water mark plus recent ids are kept, so a replay burst
// can neither grow memory nor re-render. No-seq frames pass —
// backward compatible with pre-seq servers.
export function createSseReconciler(limit = 200): SseReconciler {
  let applied: number | null = null;
  let seen: number[] = [];
  return {
    fresh(ev: SseEvent): boolean {
      const seq = sseSeq(ev);
      if (seq === null) return true;
      if (applied !== null && seq <= applied) return false;
      if (seen.includes(seq)) return false;
      applied = seq;
      seen.push(seq);
      if (seen.length > limit) seen = seen.slice(seen.length - limit);
      return true;
    },
    last(): number | null {
      return applied;
    },
    reset(): void {
      applied = null;
      seen = [];
    },
  };
}

// Coalesce a burst of token callbacks into one state commit per
// animation frame: a reattach replay lands dozens of tokens in one
// chunk, and one commit per frame renders instead of one per token.
// React owns the DOM (markdown renders per commit), so batching is
// frame-coalesced state, not raw node appends. flush() applies any
// pending text synchronously — call it when the stream ends or
// aborts so the tail is never stranded in the frame budget.
export function createTokenFlusher(
  onFlush: (text: string, count: number) => void,
): {
  push(token: string): void;
  flush(): void;
  cancel(): void;
} {
  let pending = '';
  let pendingCount = 0;
  let scheduled = 0;
  const flush = () => {
    scheduled = 0;
    if (!pending) return;
    const text = pending;
    const count = pendingCount;
    pending = '';
    pendingCount = 0;
    onFlush(text, count);
  };
  return {
    push(token: string): void {
      if (!token) return;
      pending += token;
      pendingCount += 1;
      if (!scheduled) {
        scheduled = requestAnimationFrame(flush);
      }
    },
    flush,
    cancel(): void {
      if (scheduled) cancelAnimationFrame(scheduled);
      scheduled = 0;
      pending = '';
      pendingCount = 0;
    },
  };
}

// Pump a byte stream of SSE frames, delivering each complete event
// in order. A throwing onEvent aborts the pump and reaches the
// caller; a trailing partial frame waits for more chunks. opts
// carries an optional freshness gate (issue #1208): stale frames
// after a reconnect are skipped before delivery, so they never
// re-render. Error frames always deliver — a redelivered error is
// still terminal information, never silently dropped.
export async function pumpSse(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  onEvent: (ev: SseEvent) => void,
  opts?: { isFresh?: (ev: SseEvent) => boolean },
): Promise<void> {
  const decoder = new TextDecoder();
  let buf = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const { events, rest } = parseSse(buf);
    buf = rest;
    for (const ev of events) {
      if (ev.name !== 'error' && opts?.isFresh && !opts.isFresh(ev)) continue;
      onEvent(ev);
    }
  }
}
