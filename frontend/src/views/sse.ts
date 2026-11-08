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

// Pump a byte stream of SSE frames, delivering each complete event
// in order. A throwing onEvent aborts the pump and reaches the
// caller; a trailing partial frame waits for more chunks.
export async function pumpSse(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  onEvent: (ev: SseEvent) => void,
): Promise<void> {
  const decoder = new TextDecoder();
  let buf = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const { events, rest } = parseSse(buf);
    buf = rest;
    for (const ev of events) onEvent(ev);
  }
}
