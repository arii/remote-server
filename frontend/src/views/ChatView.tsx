import { Fragment, useEffect, useRef, useState } from 'react';
import TapToFire from '../components/TapToFire';
import {
  abortChat,
  clearAgentRuns,
  deleteAgentRun,
  fetchAgentRun,
  fetchAgentRuns,
  fetchBudget,
  fetchRepoFiles,
  fetchRouteHint,
  postChatStream,
  resumeChat,
} from '../apiClient';
import type {
  AgentRun,
  AgentRunDetail,
  ApprovalPayload,
  BudgetStatus,
  ChatBackend,
  RouteHintResponse,
  TraceEvent,
} from '../apiClient';
import { loadChat, useMock, type ChatMessage } from './mockViews';
import AgentDrawer from './AgentDrawer';
import RepoLabel from '../components/RepoLabel';

// Standard chat send: Ctrl+Enter / Cmd+Enter sends from the
// prompt box; plain Enter keeps its newline.
export function isKeyboardSend(e: {
  key: string;
  ctrlKey: boolean;
  metaKey: boolean;
}): boolean {
  return e.key === 'Enter' && (e.ctrlKey || e.metaKey);
}

// Composer backend picker options: Auto keeps today's ladder,
// Local only forces Ollama (fails instead of spending), Cloud
// allowed forces Nebius for the turn.
export const CHAT_BACKEND_OPTIONS: Array<{ value: ChatBackend; label: string }> = [
  { value: 'auto', label: 'Auto' },
  { value: 'local', label: 'Local only' },
  { value: 'cloud', label: 'Cloud allowed' },
];

// One-tap prompt starters above the composer (#664). Tap sends the
// preset immediately through the normal send path; alt-click fills
// the composer for editing instead. Static set by design.
export const CHAT_PROMPT_CHIPS: Array<{ id: string; label: string; prompt: string }> = [
  { id: 'review', label: 'Review uncommitted changes', prompt: 'Review my uncommitted changes' },
  { id: 'todos', label: 'Find active TODOs', prompt: 'Find active TODOs' },
  { id: 'diff', label: 'Summarize branch diff', prompt: 'Summarize this branch diff' },
];

export function chipPickAction(text: string, altKey: boolean, streaming: boolean): 'send' | 'fill' | 'none' {
  if (streaming || !text.trim()) return 'none';
  return altKey ? 'fill' : 'send';
}

export function PromptChips({
  disabled,
  onPick,
}: {
  disabled: boolean;
  onPick: (prompt: string, altKey: boolean) => void;
}) {
  return (
    <div className="actions" role="group" aria-label="Prompt starters" data-testid="chat-prompt-chips">
      {CHAT_PROMPT_CHIPS.map((c) => (
        <button
          key={c.id}
          type="button"
          data-testid={`chat-chip-${c.id}`}
          title={`${c.label} (Alt+click fills without sending)`}
          disabled={disabled}
          onClick={(e) => onPick(c.prompt, e.altKey)}
        >
          {c.label}
        </button>
      ))}
    </div>
  );
}

// @-mention file attach (#653): typing "@" in the composer opens
// repo-file completions; accepting inserts plain "@path" text (no
// rich chips) and POST turns resolve it server-side.
export interface MentionQuery {
  start: number;
  query: string;
}

const MENTION_QUERY_CHARS = /^[A-Za-z0-9_.\-/]*$/;
const MENTION_QUERY_BOUNDARY = /[\s(\[{>"']/;

export function mentionQueryAt(text: string, cursor: number): MentionQuery | null {
  if (cursor <= 0 || cursor > text.length) return null;
  const after = text[cursor];
  if (after !== undefined && !/\s/.test(after)) return null;
  const head = text.slice(0, cursor);
  const at = head.lastIndexOf('@');
  if (at === -1) return null;
  const query = head.slice(at + 1);
  if (!MENTION_QUERY_CHARS.test(query)) return null;
  const prev = at > 0 ? (head[at - 1] ?? '') : '';
  if (prev !== '' && !MENTION_QUERY_BOUNDARY.test(prev)) return null;
  return { start: at, query };
}

export function insertMention(
  text: string,
  cursor: number,
  path: string,
): { text: string; cursor: number } | null {
  const found = mentionQueryAt(text, cursor);
  if (!found) return null;
  const inserted = `@${path} `;
  const rest = text.slice(cursor);
  const tail = rest.startsWith(' ') ? rest.slice(1) : rest;
  const next = text.slice(0, found.start) + inserted + tail;
  return { text: next, cursor: found.start + inserted.length };
}

export function MentionDropdown({
  files,
  truncated,
  active,
  onPick,
  onHover,
}: {
  files: string[];
  truncated: boolean;
  active: number;
  onPick: (path: string) => void;
  onHover: (index: number) => void;
}) {
  return (
    <div
      className="mention-list"
      role="listbox"
      aria-label="Attach a repo file"
      data-testid="chat-mention-list"
    >
      {files.map((f, i) => (
        <button
          key={f}
          type="button"
          role="option"
          aria-selected={i === active}
          data-testid="chat-mention-option"
          data-active={i === active ? 'true' : undefined}
          onMouseDown={(e) => {
            e.preventDefault();
            onPick(f);
          }}
          onMouseEnter={() => onHover(i)}
        >
          {f}
        </button>
      ))}
      {truncated && (
        <span className="row-meta" data-testid="chat-mention-more">
          Refine the prefix for more…
        </span>
      )}
    </div>
  );
}

// Composer slash commands (#669): client-only shortcuts over
// EXISTING UI actions (retry, clear, backend-picker focus,
// @-mention trigger, transcript export). Nothing new hits the
// backend; adding a command is one table entry + test.
export interface SlashActions {
  retry: () => void;
  clear: () => void;
  focusModel: () => void;
  attach: () => void;
  exportTranscript: () => void;
}

export interface SlashCommand {
  cmd: string;
  label: string;
  hint: string;
  run: (actions: SlashActions) => void;
}

export const SLASH_COMMANDS: SlashCommand[] = [
  { cmd: 'retry', label: 'Retry', hint: 'Re-send the last prompt', run: (a) => a.retry() },
  { cmd: 'clear', label: 'New chat', hint: 'Clear this conversation', run: (a) => a.clear() },
  { cmd: 'model', label: 'Model', hint: 'Focus the backend picker', run: (a) => a.focusModel() },
  { cmd: 'attach', label: 'Attach', hint: 'Mention a repo file with @', run: (a) => a.attach() },
  { cmd: 'export', label: 'Export', hint: 'Download transcript as Markdown', run: (a) => a.exportTranscript() },
];

// Leading-slash token only: the menu opens on "/re", never on
// mid-text slashes ("a/b") or once the token ends ("… --flag").
// Unknown tokens ("/foo") return the query but match nothing, so
// the menu stays shut and the text sends normally.
export function slashQueryAt(text: string): string | null {
  if (!text.startsWith('/')) return null;
  const query = text.slice(1);
  if (/\s/.test(query)) return null;
  return query;
}

export function matchSlashCommands(query: string): SlashCommand[] {
  const q = query.toLowerCase();
  return SLASH_COMMANDS.filter((c) => c.cmd.startsWith(q));
}

export function SlashMenu({
  commands,
  active,
  onPick,
  onHover,
}: {
  commands: SlashCommand[];
  active: number;
  onPick: (cmd: string) => void;
  onHover: (index: number) => void;
}) {
  return (
    <div
      className="mention-list"
      role="listbox"
      aria-label="Composer commands"
      data-testid="chat-slash-menu"
    >
      {commands.map((c, i) => (
        <button
          key={c.cmd}
          type="button"
          role="option"
          aria-selected={i === active}
          aria-label={`/${c.cmd}: ${c.hint}`}
          data-testid="chat-slash-option"
          data-active={i === active ? 'true' : undefined}
          onMouseDown={(e) => {
            e.preventDefault();
            onPick(c.cmd);
          }}
          onMouseEnter={() => onHover(i)}
        >
          /{c.cmd} <span className="row-meta">{c.hint}</span>
        </button>
      ))}
    </div>
  );
}

// Saved-template panel (#807): one-tap insert per template plus
// save-current-draft and per-template delete. Mouse-down is
// prevented like the mention/slash menus so the composer keeps
// focus (and its caret) across taps.
export function TemplatePanel({
  templates,
  canSave,
  disabled,
  onSave,
  onInsert,
  onDelete,
}: {
  templates: PromptTemplate[];
  canSave: boolean;
  disabled: boolean;
  onSave: () => void;
  onInsert: (text: string) => void;
  onDelete: (id: string) => void;
}) {
  return (
    <div
      className="actions"
      role="group"
      aria-label="Prompt templates"
      data-testid="chat-templates-panel"
    >
      <button
        type="button"
        data-testid="chat-template-save"
        title="Save the current draft as a template"
        disabled={!canSave || disabled}
        onMouseDown={(e) => {
          e.preventDefault();
        }}
        onClick={() => onSave()}
      >
        Save draft as template
      </button>
      {templates.length === 0 && (
        <span className="row-meta" data-testid="chat-templates-empty">
          No templates yet — save the draft above.
        </span>
      )}
      {templates.map((t) => (
        <div key={t.id} className="actions">
          <button
            type="button"
            data-testid={`chat-template-insert-${t.id}`}
            aria-label={`Insert template: ${t.label}`}
            title={t.text}
            disabled={disabled}
            onMouseDown={(e) => {
              e.preventDefault();
            }}
            onClick={() => onInsert(t.text)}
          >
            {t.label}
          </button>
          <button
            type="button"
            data-testid={`chat-template-delete-${t.id}`}
            aria-label={`Delete template: ${t.label}`}
            title={`Delete template: ${t.label}`}
            disabled={disabled}
            onMouseDown={(e) => {
              e.preventDefault();
            }}
            onClick={() => onDelete(t.id)}
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}

// Voice dictation (#659): tap-to-dictate into the composer via the
// Web Speech API. Purely client-side: no backend, no deps, no keys.
// Chrome/Android ship SpeechRecognition; Firefox and iOS Safari do
// not — the capability check hides the button there (no polyfill,
// no dead button, nothing to explain in the UI).
export interface DictationRecognition {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  onresult: ((ev: DictationResultEvent) => void) | null;
  onerror: ((ev: DictationErrorEvent) => void) | null;
  onend: (() => void) | null;
  start: () => void;
  stop: () => void;
}

export type DictationRecognitionCtor = new () => DictationRecognition;

// The dom lib has no SpeechRecognition types, so the constructor is
// read off window structurally. Standard name first, webkit-prefixed
// fallback second (older Chrome/Android).
export function dictationCtor(): DictationRecognitionCtor | null {
  if (typeof window === 'undefined') return null;
  const w = window as unknown as Record<string, unknown>;
  const ctor = w.SpeechRecognition ?? w.webkitSpeechRecognition;
  return typeof ctor === 'function'
    ? (ctor as unknown as DictationRecognitionCtor)
    : null;
}

export function dictationSupported(): boolean {
  return dictationCtor() !== null;
}

// One recognition result event, structurally typed: results from
// resultIndex on are new since the previous event; each carries
// isFinal plus transcript alternatives (first only is used).
export interface DictationResultEvent {
  resultIndex: number;
  results: ArrayLike<{ isFinal: boolean; 0: { transcript: string } }>;
}

export interface DictationErrorEvent {
  error: string;
}

// Split one event's new results into final vs in-flight text.
// Interim text is a live preview of an utterance that a later final
// result replaces — never appended twice (see applyDictationResult).
export function extractDictationText(ev: DictationResultEvent): {
  finalText: string;
  interimText: string;
} {
  let finalText = '';
  let interimText = '';
  for (let i = ev.resultIndex; i < ev.results.length; i++) {
    const res = ev.results[i];
    const transcript = res[0]?.transcript ?? '';
    if (res.isFinal) finalText += transcript;
    else interimText += transcript;
  }
  return { finalText, interimText };
}

// Dictation lands as ordinary composer text: appended with a single
// separating space, editable before send. The backend picker, cost
// line, @-mentions, and send flow all behave identically.
export function appendDictationText(current: string, addition: string): string {
  const text = addition.trim();
  if (!text) return current;
  if (current.length === 0 || /\s$/.test(current)) return current + text;
  return `${current} ${text}`;
}

// Fold one result event into the composer: strip the previous
// event's interim preview first (an interim preview is replaced by
// its final, never duplicated), then append this event's new finals
// plus its new interim preview. Only the interim suffix is tracked
// for rebasing — committed finals are never stripped, so later
// utterances accumulate. Returns the composer text plus the exact
// interim suffix this call inserted. Typed-during-dictation text
// survives — the strip only fires when the composer still ends with
// our last preview — except the in-flight interim then stays AND its
// final appends; accepted (the text is editable before send, and
// tap-to-dictate sessions are short).
export function applyDictationResult(
  prev: string,
  inserted: string,
  finalText: string,
  interimText: string,
): { next: string; inserted: string } {
  const base =
    inserted.length > 0 && prev.endsWith(inserted)
      ? prev.slice(0, prev.length - inserted.length)
      : prev;
  const finalChunk = finalText.trim();
  const interimChunk = interimText.trim();
  let next = base;
  if (finalChunk) next = appendDictationText(next, finalChunk);
  if (!interimChunk) return { next, inserted: '' };
  const withPreview = appendDictationText(next, interimChunk);
  return { next: withPreview, inserted: withPreview.slice(next.length) };
}

// One-line composer hints for recognition failures: never toasts,
// never send-blocking. Denied-mic names the browser permission path.
// 'aborted' is tap-to-stop (and the stop behind unmount), not an
// error; unknown codes stay silent rather than guessing.
export function dictationErrorHint(error: string): string | null {
  switch (error) {
    case 'not-allowed':
    case 'service-not-allowed':
      return 'Mic blocked — allow microphone access in the browser site settings, then tap Dictate again.';
    case 'no-speech':
      return 'No speech heard — tap Dictate and try again.';
    case 'network':
      return 'Dictation needs a network connection — check connectivity and retry.';
    case 'audio-capture':
      return 'No microphone found — check the device input and retry.';
    default:
      return null;
  }
}

export function DictationHint({ text }: { text: string }) {
  return (
    <p className="row-meta" data-testid="chat-dictate-hint" role="status">
      {text}
    </p>
  );
}

// Mic button beside Send. Rendered ONLY where the API exists —
// elsewhere this returns null (no dead button, no polyfill). While
// live, the pulsing dot + aria-pressed carry the recording state.
export function DictateButton({
  recording,
  disabled,
  onToggle,
}: {
  recording: boolean;
  disabled: boolean;
  onToggle: () => void;
}) {
  if (!dictationSupported()) return null;
  return (
    <button
      type="button"
      data-testid="chat-dictate"
      aria-pressed={recording}
      aria-label={recording ? 'Stop dictation' : 'Dictate with voice'}
      title={recording ? 'Stop dictation' : 'Dictate with voice'}
      disabled={disabled}
      onClick={() => onToggle()}
    >
      {recording && <span className="dot dot-bad dictate-rec" aria-hidden="true" />}
      {recording ? 'Stop' : 'Dictate'}
    </button>
  );
}

// Transcript export (#669): turns in order as Markdown. Assistant
// turns reuse cleanAgentText (no INTERRUPT/trace blobs).
// ChatMessage carries no timestamps and no per-turn traces, so the
// export omits both rather than inventing them.
export function transcriptMarkdown(messages: ChatMessage[]): string {
  const lines = ['# Chat transcript', ''];
  for (const m of messages) {
    lines.push(m.role === 'user' ? '## You' : '## Assistant', '');
    lines.push(m.role === 'assistant' ? cleanAgentText(m.text) : m.text, '');
  }
  return lines.join('\n');
}

export function transcriptFilename(when: Date = new Date()): string {
  return `chat-${when.toISOString().slice(0, 10)}.md`;
}

export function downloadTranscript(messages: ChatMessage[]): void {
  const blob = new Blob([transcriptMarkdown(messages)], { type: 'text/markdown' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = transcriptFilename();
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// Streamed tokens carry raw agent stdout, which ends with the
// INTERRUPT blob on approval turns. The backend strips markers
// from done.text; strip them here too so the chat log stays human.
export function cleanAgentText(text: string): string {
  return text
    .split('\n')
    .filter((ln) => !ln.startsWith('INTERRUPT:') && !ln.startsWith('trace:'))
    .join('\n')
    .trim();
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : '';
}

function num(v: unknown): number {
  return typeof v === 'number' ? v : 0;
}

// One trace event as a terse timeline line (mirrors trace_view text).
export function traceLine(ev: TraceEvent, t0: number): string {
  const dt = num(ev.ts) - t0;
  const at = `+${dt >= 0 ? dt.toFixed(1) : '0.0'}s`;
  switch (ev.type) {
    case 'run_start':
      return `${at} START ${str(ev.script)} thread=${str(ev.thread)} model=${str(ev.model)}`;
    case 'model_request':
      return `${at} LLM req n_msgs=${num(ev.n_messages)}`;
    case 'model_response': {
      const u = (ev.usage ?? {}) as Record<string, unknown>;
      return `${at} LLM resp in=${num(u.input_tokens)} out=${num(u.output_tokens)} tot=${num(u.total_tokens)}`;
    }
    case 'tool_call': {
      const dur = num(ev.duration_s).toFixed(2);
      if (ev.ok) {
        return `${at} ok ${str(ev.name)} ${dur}s ${str(ev.args_preview)} -> ${str(ev.result_preview)}`.slice(0, 200);
      }
      return `${at} ERR ${str(ev.name)} ${dur}s err=${str(ev.error)}`.slice(0, 200);
    }
    case 'run_end':
      return `${at} END ok=${String(ev.ok)} dur=${num(ev.duration_s).toFixed(2)}s`;
    default:
      return `${at} ? ${ev.type}`;
  }
}

const NARROW_VIEWPORT_QUERY = '(max-width: 767px)';

export interface TraceSummary {
  action: string;
  timing: string;
  status: 'ok' | 'failed' | 'info';
}

function offsetClock(ev: TraceEvent, t0: number): string {
  const dt = num(ev.ts) - t0;
  return `+${dt >= 0 ? dt.toFixed(1) : '0.0'}s`;
}

// Collapsed summary row for narrow viewports: action + timing +
// ok/failed status, derived from existing event fields only.
export function traceSummary(ev: TraceEvent, t0: number): TraceSummary {
  const at = offsetClock(ev, t0);
  switch (ev.type) {
    case 'run_start':
      return { action: `START ${str(ev.script) || '?'}`, timing: at, status: 'info' };
    case 'model_request':
      return { action: 'LLM req', timing: at, status: 'info' };
    case 'model_response':
      return { action: 'LLM resp', timing: at, status: 'info' };
    case 'tool_call': {
      const dur = typeof ev.duration_s === 'number' ? `${ev.duration_s.toFixed(2)}s` : at;
      const status = ev.ok === true ? 'ok' : ev.ok === false ? 'failed' : 'info';
      return { action: str(ev.name) || '?', timing: dur, status };
    }
    case 'run_end': {
      const dur = typeof ev.duration_s === 'number' ? `${ev.duration_s.toFixed(2)}s` : at;
      const status = ev.ok === true ? 'ok' : ev.ok === false ? 'failed' : 'info';
      return { action: 'END', timing: dur, status };
    }
    default:
      return { action: `? ${ev.type}`, timing: at, status: 'info' };
  }
}

function useIsNarrowViewport(): boolean {
  const [narrow, setNarrow] = useState(false);
  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return;
    const mq = window.matchMedia(NARROW_VIEWPORT_QUERY);
    setNarrow(mq.matches);
    const onChange = (e: MediaQueryListEvent) => setNarrow(e.matches);
    if (typeof mq.addEventListener === 'function') {
      mq.addEventListener('change', onChange);
      return () => mq.removeEventListener('change', onChange);
    }
    mq.addListener(onChange);
    return () => mq.removeListener(onChange);
  }, []);
  return narrow;
}

// Run-timeline trace: the full expanded log on desktop, collapsed
// per-event summary rows (tap to expand inline) on narrow viewports.
// The narrow prop overrides the media query (used by tests).
export function TraceTimeline({
  trace,
  t0,
  narrow,
}: {
  trace: TraceEvent[];
  t0: number;
  narrow?: boolean;
}) {
  const queryNarrow = useIsNarrowViewport();
  const isNarrow = narrow ?? queryNarrow;
  if (!isNarrow) {
    return (
      <pre className="row-meta">
        {trace.length > 0
          ? trace.map((ev) => traceLine(ev, t0)).join('\n')
          : 'No trace events recorded.'}
      </pre>
    );
  }
  if (trace.length === 0) {
    return <p className="row-meta">No trace events recorded.</p>;
  }
  return (
    <ol className="trace-fold" data-testid="chat-trace-fold">
      {trace.map((ev, i) => {
        const s = traceSummary(ev, t0);
        const dotCls = s.status === 'ok' ? 'dot-ok' : s.status === 'failed' ? 'dot-bad' : '';
        return (
          // Trace events carry no ids; order is stable.
          // eslint-disable-next-line react/no-array-index-key
          <li key={i} className="trace-fold-row">
            <details>
              <summary>
                <span className={`dot ${dotCls}`} aria-hidden="true" />
                <span className="trace-fold-action">{s.action}</span>
                <span className="trace-fold-timing">{s.timing}</span>
                {s.status !== 'info' && (
                  <span className={`trace-fold-status ${s.status}`}>{s.status}</span>
                )}
              </summary>
              <pre className="row-meta">{traceLine(ev, t0)}</pre>
            </details>
          </li>
        );
      })}
    </ol>
  );
}

export function runMeta(run: AgentRun): string {
  const when = run.started_ts
    ? new Date(run.started_ts * 1000).toLocaleString()
    : 'unknown time';
  const tools = run.tools.length > 0 ? ` · tools ${run.tools.join(',')}` : '';
  return `${run.script} · ${modelSlot(run)} · ${run.ok ? 'ok' : 'FAILED'} · ${runLatency(run)} · ${run.tokens} tok · ${runCost(run)}${tools} · ${when}`;
}

// Latency slot: recorded turn seconds, or an em dash when the
// ledger row predates duration tracking or the record failed.
export function runLatency(run: AgentRun): string {
  return typeof run.duration_s === 'number' ? `${run.duration_s}s` : '—';
}

// Model slot with a distinct provider lead: ledger labels read
// `{provider}:{model}`, so `ollama:qwen3:8b` renders as
// `ollama · qwen3:8b` instead of repeating the prefix. Rows
// without a known prefix show the model verbatim, no slot.
function modelSlot(run: AgentRun): string {
  if (run.provider && run.model.startsWith(`${run.provider}:`)) {
    const short = run.model.slice(run.provider.length + 1);
    if (short) return `${run.provider} · ${short}`;
  }
  return run.provider ? `${run.provider} · ${run.model}` : run.model;
}

// Compact token count for spoken labels: 11089 -> 11k, 96 -> 96.
function compactTokens(n: number): string {
  return n >= 1000 ? `${Math.round(n / 1000)}k` : `${n}`;
}

// Concise accessible name for a Past runs card button. The card's
// full preview + metadata line used to be the name (~250 chars per
// Tab stop); the label names id, status, duration, tokens, and day
// instead. Card text stays visible (and browse-readable): the label
// only replaces the Tab-stop name, so nothing is aria-hidden.
export function runButtonLabel(run: AgentRun): string {
  const when = run.started_ts
    ? new Date(run.started_ts * 1000).toISOString().slice(0, 10)
    : 'unknown time';
  return `Run ${run.id}, ${run.ok ? 'ok' : 'failed'}, ${runLatency(run)}, ${compactTokens(run.tokens)} tokens, ${when}`;
}

// Dollars trimmed for phone lines: $5 not $5.0000, $0.0008 kept.
function usd(n: number): string {
  return `$${n.toFixed(4).replace(/\.?0+$/, '')}`;
}

// Per-row cost slot: local runs are free, metered runs show the
// recorded/derived quote, unrated metered runs stay honest with a
// bare tag instead of an invented number.
export function runCost(run: AgentRun): string {
  if (!run.metered) return 'free';
  if (run.cost_usd === null || run.cost_usd === undefined) return 'metered';
  return usd(run.cost_usd);
}

// Pre-send composer line from a route hint: backend + cost signal.
// Same vocabulary as runCost (free / metered / $), with the card
// per-1M range since usage is unknown before generation. Failures
// stay visible but short: signal only, never a send block.
export function hintLine(hint: RouteHintResponse): string {
  if (!hint.ok) {
    const err = hint.error.trim().slice(0, 120) || 'unknown reason';
    return `Next reply unavailable (${err})`;
  }
  const via = `Next reply via ${hint.provider}:${hint.model}`;
  if (!hint.metered) return `${via} · free`;
  if (hint.rate_in_per_1m === null || hint.rate_out_per_1m === null) {
    return `${via} · metered`;
  }
  return `${via} · metered ~${usd(hint.rate_in_per_1m)}–${usd(hint.rate_out_per_1m)}/1M`;
}

// Remaining-budget line for the runs header. Uncapped is stated
// plainly (an unset cap is itself useful to see); a hit cap names
// the block and the figures.
export function budgetLine(budget: BudgetStatus): string {
  if (budget.cap_usd === null || budget.cap_usd === undefined) {
    return 'No spend cap set';
  }
  if (budget.blocked) {
    return `Budget cap hit — metered calls blocked (${usd(budget.spent_usd)} of ${usd(budget.cap_usd)} used)`;
  }
  return `Budget ${usd(budget.remaining_usd ?? 0)} left of ${usd(budget.cap_usd)}`;
}

export interface MeteredTotal {
  total: number;
  unknown: number;
  free: number;
}

// Running metered total over the listed runs. Rounded to whole
// microdollars so the total matches the row sums exactly.
export function meteredTotal(runs: AgentRun[]): MeteredTotal {
  let total = 0;
  let unknown = 0;
  let free = 0;
  for (const r of runs) {
    if (!r.metered) {
      free += 1;
    } else if (r.cost_usd === null || r.cost_usd === undefined) {
      unknown += 1;
    } else {
      total += r.cost_usd;
    }
  }
  return { total: Math.round(total * 1e6) / 1e6, unknown, free };
}

// One quiet Past-runs refetch after a completed send: the backend
// has recorded a new run, so the list would otherwise stay stale
// until a remount. The fresh list replaces, never appends (no
// duplicate rows); failures stay silent — a stale list beats a
// bogus chat error.
export async function refreshRunsQuiet(
  fetchRuns: () => Promise<{ runs: AgentRun[] }>,
  setRuns: (runs: AgentRun[]) => void,
): Promise<void> {
  try {
    setRuns((await fetchRuns()).runs);
  } catch {
    // Past runs stay as they were; live chat still works.
  }
}

export interface RunDecision {
  what: string;
  why: string;
}

// Per-turn what+why lines deep_chat records into extra.decisions.
// Older rows predate them; malformed entries never break the pane.
export function runDecisions(entry: Record<string, unknown>): RunDecision[] {
  const extra = entry.extra as Record<string, unknown> | undefined;
  const raw = extra?.decisions;
  if (!Array.isArray(raw)) return [];
  return raw
    .filter(
      (d): d is { what: unknown; why: unknown } =>
        typeof d === 'object' && d !== null,
    )
    .filter((d) => typeof d.what === 'string' && typeof d.why === 'string')
    .map((d) => ({ what: d.what as string, why: d.why as string }));
}

// Expanded detail for one past run: decisions + full trace.
// Rendered as the list item right after its row so the trace lands
// adjacent to the click target instead of below the whole list.
function RunDetail({ detail, t0 }: { detail: AgentRunDetail; t0: number }) {
  return (
    <li className="detail" data-testid="chat-run-detail">
      <span className="row-title">{detail.id}</span>
      {runDecisions(detail.entry).length > 0 && (
        <div data-testid="chat-run-decisions">
          <span className="row-title">Decisions</span>
          <ul className="session-list">
            {runDecisions(detail.entry).map((d, i) => (
              <li key={i} data-testid={`chat-run-decision-${i}`}>
                <span>{d.what}</span>{' '}
                <span className="row-meta">{d.why}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      <TraceTimeline trace={detail.trace} t0={t0} />
    </li>
  );
}

export function RunsPane({
  runs,
  selected,
  detail,
  onSelect,
  onContinue,
  onDelete,
  onClearFinished,
  notice,
  budget,
}: {
  runs: AgentRun[];
  selected: string | null;
  detail: AgentRunDetail | null;
  onSelect: (id: string) => void;
  onContinue: (id: string) => void;
  onDelete: (id: string) => void;
  onClearFinished: () => void;
  notice: string | null;
  budget: BudgetStatus | null;
}) {
  if (runs.length === 0) return null;
  const t0 = detail && detail.trace.length > 0 ? num(detail.trace[0].ts) : 0;
  const metered = meteredTotal(runs);
  return (
    <section data-testid="chat-runs">
      <h3>Past runs</h3>
      <p className="row-meta" data-testid="chat-runs-metered-total">
        Metered total {usd(metered.total)}
        {metered.unknown > 0 &&
          ` (+${metered.unknown} unknown rate${metered.unknown === 1 ? '' : 's'})`}
      </p>
      {budget && (
        <p className="row-meta" data-testid="chat-runs-budget">
          {budgetLine(budget)}
        </p>
      )}
      <div className="actions">
        <TapToFire
          label="Clear finished"
          confirm="This clears all finished runs."
          testid="chat-runs-clear"
          onFire={onClearFinished}
        />
      </div>
      {notice && (
        <p className="row-meta" data-testid="chat-runs-notice" role="status">
          {notice}
        </p>
      )}
      <ul className="session-list">
        {runs.map((r) => (
          <Fragment key={r.id}>
            <li className={`detail${selected === r.id ? ' selected' : ''}`}>
              <button
                type="button"
                data-testid={`chat-run-${r.id}`}
                aria-label={runButtonLabel(r)}
                aria-pressed={selected === r.id}
                onClick={() => onSelect(r.id)}
              >
                <span className="row-title">{r.preview || r.id}</span>{' '}
                <span className="row-meta">
                  {r.id} · {runMeta(r)}
                </span>
              </button>
              <div className="actions">
                <button
                  type="button"
                  data-testid={`chat-run-continue-${r.id}`}
                  disabled={!r.has_prompt}
                  title={r.has_prompt ? undefined : 'No stored prompt for this run'}
                  aria-label={`Continue ${r.id}`}
                  onClick={() => onContinue(r.id)}
                >
                  Continue
                </button>
                <TapToFire
                  label="Delete"
                  name={`Delete ${r.id}`}
                  confirm="This cannot be undone."
                  testid={`chat-run-delete-${r.id}`}
                  onFire={() => onDelete(r.id)}
                />
              </div>
            </li>
            {selected === r.id && detail && <RunDetail detail={detail} t0={t0} />}
          </Fragment>
        ))}
      </ul>
    </section>
  );
}

function requestSummary(args: Record<string, unknown>): string {
  if (args.kind === 'td') return `Run: ${str(args.command)}`;
  if (args.kind === 'issue') return `File issue: ${str(args.title)}`;
  return 'Proposed write';
}

export function ApprovalCard({
  approval,
  decided,
  onDecide,
}: {
  approval: ApprovalPayload;
  decided: string | null;
  onDecide: (decision: 'approve' | 'reject') => void;
}) {
  const reqs = approval.action_requests ?? [];
  return (
    <div className="detail" data-testid="chat-approval" role="alert">
      <span className="row-title">Approval needed</span>
      <ul className="session-list">
        {reqs.map((r, i) => (
          // eslint-disable-next-line react/no-array-index-key
          <li key={i} className="row-meta">
            {requestSummary(r.args ?? {})}
          </li>
        ))}
      </ul>
      {(approval.proposals ?? []).map((p, i) => (
        // eslint-disable-next-line react/no-array-index-key
        <pre key={i} className="row-meta">
          {p}
        </pre>
      ))}
      {decided ? (
        <p className="row-meta" data-testid="chat-approval-decided">
          {decided}
        </p>
      ) : (
        <div className="actions">
          <TapToFire
            label="Approve"
            confirm="The proposed actions will run."
            testid="chat-approve"
            onFire={() => onDecide('approve')}
          />
          <button type="button" data-testid="chat-reject" onClick={() => onDecide('reject')}>
            Reject
          </button>
        </div>
      )}
    </div>
  );
}

// True while the stream is open but no token has landed yet: the
// assistant placeholder is still empty, so show a typing indicator.
export function awaitingFirstToken(
  streaming: boolean,
  messages: ChatMessage[],
): boolean {
  if (!streaming || messages.length === 0) return false;
  const last = messages[messages.length - 1];
  return last.role === 'assistant' && last.text.length === 0;
}

export function TypingIndicator() {
  return (
    <li
      className="detail typing"
      data-testid="chat-typing"
      aria-live="polite"
      aria-label="Assistant is typing"
    >
      <span className="row-title">Assistant</span>
      <span className="typing-dots" aria-hidden="true">
        <span />
        <span />
        <span />
      </span>
    </li>
  );
}

// No-event watchdog: abort a stream that goes silent this long. Routine
// local prefill exceeds a minute; the backend bounds nothing on this
// path, so 3x that margin separates stall from slowness.
export const STALL_SECONDS = 180;

export function isStalled(lastEventMs: number, nowMs: number): boolean {
  return nowMs - lastEventMs > STALL_SECONDS * 1000;
}

// Retry re-sends the last prompt after a failed stream (deploy cut the
// connection, backend hiccup). Shown only when there is something to
// re-send and nothing running — never auto-runs.
export function shouldShowChatRetry(
  error: string | null,
  streaming: boolean,
  lastSent: string,
): boolean {
  return (
    !streaming && error !== null && error.length > 0 && lastSent.trim().length > 0
  );
}

// Retry reuses the pending exchange: drop the trailing failed pair
// (the last user message equal to lastSent plus its assistant entry,
// empty or partial) so the resend appends exactly one fresh pair.
// Anything else returns untouched — retry then falls back to a
// plain append rather than deleting the wrong messages.
export function stripFailedRetryExchange(
  messages: ChatMessage[],
  lastSent: string,
): ChatMessage[] {
  const n = messages.length;
  if (
    n >= 2 &&
    messages[n - 2].role === 'user' &&
    messages[n - 2].text === lastSent &&
    messages[n - 1].role === 'assistant'
  ) {
    return messages.slice(0, n - 2);
  }
  return messages;
}

// Live-conversation persistence (issue #573): App renders one tab
// at a time, so ChatView unmounts on every tab switch — messages
// used to die with it. The settled exchange persists to
// sessionStorage on every change and restores on mount, so
// Chat->Now->Chat keeps the conversation within the session. Past
// runs stay the archive; the /clear path empties the store along
// with the state. Mock mode neither reads nor writes, so ?mock=1
// demos never touch the real session. Storage failures (private
// mode) only drop persistence — the live conversation still works.
export const CHAT_MESSAGES_KEY = 'buttons.chat.messages.v1';

export function isChatMessage(value: unknown): value is ChatMessage {
  if (!value || typeof value !== 'object') return false;
  const m = value as Record<string, unknown>;
  return (m.role === 'user' || m.role === 'assistant') && typeof m.text === 'string';
}

export function loadChatMessages(): ChatMessage[] {
  try {
    if (typeof sessionStorage === 'undefined') return [];
    const raw = sessionStorage.getItem(CHAT_MESSAGES_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(isChatMessage);
  } catch {
    return [];
  }
}

export function saveChatMessages(messages: ChatMessage[]): void {
  try {
    if (typeof sessionStorage === 'undefined') return;
    sessionStorage.setItem(CHAT_MESSAGES_KEY, JSON.stringify(messages));
  } catch {
    // Persistence is best-effort; the live conversation still works.
  }
}

// Conversation thread id (issue #467): the deep_chat thread behind
// the visible log. Persisted beside the #573 message snapshot so a
// tab-switch remount restores the same thread instead of silently
// opening a cold one under the old log. Mock mode neither reads nor
// writes, matching the message snapshot.
export const CHAT_SESSION_KEY = 'buttons.chat.session.v1';

export function loadChatSessionId(): string | null {
  try {
    if (typeof sessionStorage === 'undefined') return null;
    const raw = sessionStorage.getItem(CHAT_SESSION_KEY);
    return raw && raw.length > 0 ? raw : null;
  } catch {
    return null;
  }
}

export function saveChatSessionId(sessionId: string): void {
  try {
    if (typeof sessionStorage === 'undefined') return;
    sessionStorage.setItem(CHAT_SESSION_KEY, sessionId);
  } catch {
    // Persistence is best-effort; the live conversation still works.
  }
}

export function clearChatSessionId(): void {
  try {
    if (typeof sessionStorage === 'undefined') return;
    sessionStorage.removeItem(CHAT_SESSION_KEY);
  } catch {
    // Persistence is best-effort; the live conversation still works.
  }
}

// Composer draft autosave (issue #402): the prompt textbox is local
// useState, so a tab switch (unmount/remount) used to destroy the
// draft. The draft persists to sessionStorage on every change and
// restores on mount; sending clears it. Mirrors the #573 message
// snapshot: same typeof/try-catch guards, mock mode neither reads
// nor writes, storage failures only drop persistence.
export const CHAT_DRAFT_KEY = 'buttons.chat.draft.v1';

export function loadChatDraft(): string {
  try {
    if (typeof sessionStorage === 'undefined') return '';
    return sessionStorage.getItem(CHAT_DRAFT_KEY) ?? '';
  } catch {
    return '';
  }
}

export function saveChatDraft(draft: string): void {
  try {
    if (typeof sessionStorage === 'undefined') return;
    sessionStorage.setItem(CHAT_DRAFT_KEY, draft);
  } catch {
    // Persistence is best-effort; the live composer still works.
  }
}

export function clearChatDraft(): void {
  try {
    if (typeof sessionStorage === 'undefined') return;
    sessionStorage.removeItem(CHAT_DRAFT_KEY);
  } catch {
    // Persistence is best-effort; the live composer still works.
  }
}

// Saved prompt templates (#807): user-saved composer snippets in
// localStorage (survive reload + relaunch), seeded with starters on
// first run. One-tap insert at the caret; a {text} slot in the
// template lands the caret at the slot instead of past the insert.
// Seeds are ordinary entries once stored — deleting them all leaves
// an empty store (no reseed). Same typeof/try-catch guards as the
// sessionStorage snapshots above; mock mode neither reads nor writes.
export interface PromptTemplate {
  id: string;
  label: string;
  text: string;
}

export const CHAT_TEMPLATES_KEY = 'buttons.chat.templates.v1';
export const TEMPLATE_TEXT_SLOT = '{text}';

export const SEED_TEMPLATES: ReadonlyArray<PromptTemplate> = [
  {
    id: 'standup',
    label: 'Standup',
    text: 'Write a standup summary: finished, next, blockers. {text}',
  },
  {
    id: 'review',
    label: 'Review request',
    text: 'Review my uncommitted changes for bugs, edge cases, and missing tests. {text}',
  },
  {
    id: 'explain',
    label: 'Explain',
    text: 'Explain how {text} works in this repo, with file references.',
  },
];

export function isPromptTemplate(value: unknown): value is PromptTemplate {
  if (!value || typeof value !== 'object') return false;
  const t = value as Record<string, unknown>;
  return (
    typeof t.id === 'string' &&
    typeof t.label === 'string' &&
    typeof t.text === 'string'
  );
}

export function loadPromptTemplates(): PromptTemplate[] {
  try {
    if (typeof localStorage === 'undefined') return [...SEED_TEMPLATES];
    const raw = localStorage.getItem(CHAT_TEMPLATES_KEY);
    if (!raw) return [...SEED_TEMPLATES];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [...SEED_TEMPLATES];
    return parsed.filter(isPromptTemplate);
  } catch {
    return [...SEED_TEMPLATES];
  }
}

export function savePromptTemplates(templates: PromptTemplate[]): void {
  try {
    if (typeof localStorage === 'undefined') return;
    localStorage.setItem(CHAT_TEMPLATES_KEY, JSON.stringify(templates));
  } catch {
    // Persistence is best-effort; the live panel still works.
  }
}

let templateSeq = 0;

export function templateId(): string {
  templateSeq += 1;
  const rand = Math.floor(Math.random() * 1296).toString(36);
  return `t${Date.now().toString(36)}${templateSeq.toString(36)}${rand}`;
}

export function templateLabelForDraft(draft: string, max = 40): string | null {
  const first = draft.split('\n', 1)[0].trim();
  if (!first) return null;
  return first.length > max ? `${first.slice(0, max - 1)}…` : first;
}

export function makeTemplateFromDraft(draft: string): PromptTemplate | null {
  const text = draft.trim();
  const label = templateLabelForDraft(draft);
  if (!text || !label) return null;
  return { id: templateId(), label, text };
}

// Insert a template at the caret: the snippet splices in and the
// caret lands past the insert — or at the first {text} slot, whose
// markers are consumed so typing flows naturally. Markers stay
// intact in the stored template; only the insert consumes them.
export function insertTemplateAt(
  text: string,
  caret: number,
  snippet: string,
): { text: string; caret: number } {
  const at = Math.max(0, Math.min(caret, text.length));
  const slot = snippet.indexOf(TEMPLATE_TEXT_SLOT);
  const consumed = snippet.split(TEMPLATE_TEXT_SLOT).join('');
  const next = text.slice(0, at) + consumed + text.slice(at);
  return { text: next, caret: slot === -1 ? at + consumed.length : at + slot };
}

// Persisted snapshots hold the settled conversation only: a trailing
// empty assistant entry is the live typing placeholder (meaningless
// once streaming stops), so it never reaches sessionStorage. The live
// state keeps it — only the snapshot is trimmed.
export function settledChatMessages(messages: ChatMessage[]): ChatMessage[] {
  const n = messages.length;
  if (n === 0) return messages;
  const last = messages[n - 1];
  if (last.role === 'assistant' && last.text === '') return messages.slice(0, n - 1);
  return messages;
}

// Edit recalls a sent prompt into the composer for fix-and-resend.
// Available on every user bubble once the stream settles.
export function shouldShowChatEdit(streaming: boolean): boolean {
  return !streaming;
}

// Regenerate offers a second chance on a completed reply (a weak
// local-model answer), reusing the retry/send path. Sibling to
// shouldShowChatRetry: never while streaming, never on an empty
// reply, never without a recoverable prompt — and still never
// auto-runs. The failure-retry gate above is untouched.
export function shouldShowChatRegenerate(
  streaming: boolean,
  replyText: string,
  promptText: string,
): boolean {
  return (
    !streaming && replyText.trim().length > 0 && promptText.trim().length > 0
  );
}

// Nearest user prompt above an assistant bubble: regenerate re-sends
// this turn's prompt, not the global lastSent, so older turns keep
// their own second chance. '' when no user turn precedes the bubble.
export function turnPrompt(messages: ChatMessage[], index: number): string {
  for (let i = index - 1; i >= 0; i--) {
    if (messages[i].role === 'user') return messages[i].text;
  }
  return '';
}

// Streaming progress line: elapsed always, plus phase and token count
// once known. "Working… 45s · prefill · 12 tok".
export function streamProgress(
  elapsedSec: number,
  phase: string | null,
  tokens: number,
): string {
  let line = `Working… ${elapsedSec}s`;
  if (phase) line += ` · ${phase}`;
  if (tokens > 0) line += ` · ${tokens} tok`;
  return line;
}

// Ticks every second while streaming, so deliberately not a live
// region (the sr-only phase announcer carries screen-reader updates).
export function StreamProgress({
  elapsed,
  phase,
  tokens,
}: {
  elapsed: number;
  phase: string | null;
  tokens: number;
}) {
  return (
    <p className="row-meta" data-testid="chat-progress">
      {streamProgress(elapsed, phase, tokens)}
    </p>
  );
}

// Turn list with per-bubble second chances: Edit on sent prompts,
// Regenerate on completed replies. Both hide while streaming;
// Regenerate also hides when the turn's prompt is not recoverable,
// so there are never dead buttons. Indexed testids follow the
// chat-msg-<role>-<i> neighbor convention.
export function ChatHistory({
  messages,
  streaming,
  onEdit,
  onRegenerate,
}: {
  messages: ChatMessage[];
  streaming: boolean;
  onEdit: (text: string) => void;
  onRegenerate: (text: string) => void;
}) {
  const typing = awaitingFirstToken(streaming, messages);
  const shown = typing ? messages.slice(0, -1) : messages;
  return (
    <ul className="session-list" data-testid="chat-history">
      {shown.map((m, i) => (
        <li key={i} className="detail" data-testid={`chat-msg-${m.role}-${i}`}>
          <span className="row-title">{m.role === 'user' ? 'You' : 'Assistant'}</span>
          <p>{m.role === 'assistant' ? cleanAgentText(m.text) : m.text}</p>
          {m.role === 'user' && shouldShowChatEdit(streaming) && (
            <div className="actions">
              <button type="button" data-testid={`chat-edit-${i}`} onClick={() => onEdit(m.text)}>
                Edit
              </button>
            </div>
          )}
          {m.role === 'assistant' &&
            shouldShowChatRegenerate(streaming, m.text, turnPrompt(shown, i)) && (
              <div className="actions">
                <button
                  type="button"
                  data-testid={`chat-regenerate-${i}`}
                  onClick={() => onRegenerate(turnPrompt(shown, i))}
                >
                  Regenerate
                </button>
              </div>
            )}
        </li>
      ))}
      {typing && <TypingIndicator />}
    </ul>
  );
}

function newSessionId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return crypto.randomUUID().replace(/-/g, '').slice(0, 12);
  }
  return Math.random().toString(16).slice(2, 14).padEnd(12, '0');
}

// One thread per conversation (issue #467): every send used to mint
// a fresh session id, so each message opened a cold deep_chat thread
// while the log implied one conversation. Sends now reuse the live
// id (restored from the session snapshot across remounts); only New
// chat resets it. Mirrors the AgentDrawer sidRef reuse pattern.
export function resolveChatSessionId(
  current: string | null,
  stored: string | null,
  mint: () => string,
): string {
  if (current) return current;
  if (stored) return stored;
  return mint();
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

function parseSse(buffer: string): { events: SseEvent[]; rest: string } {
  const parts = buffer.split('\n\n');
  const rest = parts.pop() ?? '';
  const events = parts
    .map(parseSsePart)
    .filter((e): e is SseEvent => e !== null);
  return { events, rest };
}

function parseJson<T>(data: string): T | null {
  try {
    return JSON.parse(data) as T;
  } catch {
    return null;
  }
}

export async function pumpStream(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  onToken: (token: string) => void,
  onApproval: (payload: ApprovalPayload) => void,
  onPhase: (message: string | null) => void,
  onDone: (text: string) => void,
  onEvent: () => void,
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
      onEvent();
      if (ev.name === 'error') {
        const message = parseJson<{ error?: string }>(ev.data)?.error;
        throw new Error(message && message.length > 0 ? message : ev.data);
      }
      if (ev.name === 'token') {
        onToken(parseJson<{ token?: string }>(ev.data)?.token ?? '');
      }
      if (ev.name === 'approval') {
        const payload = parseJson<ApprovalPayload>(ev.data);
        if (payload) onApproval(payload);
      }
      if (ev.name === 'phase') {
        const msg = parseJson<{ message?: string }>(ev.data)?.message;
        onPhase(msg && msg.length > 0 ? msg : null);
      }
      if (ev.name === 'done') {
        onPhase(null);
        onDone(parseJson<{ text?: string }>(ev.data)?.text ?? '');
      }
    }
  }
}

// Seed staging (#507): Briefing/Now/Review "Ask agent" seeds land
// in the composer for review and edit — never auto-sent, so one
// tap can't start a metered turn. Returns the text to stage, or
// null when there is no seed or it was already staged.
export function stageSeed(seed: string | null, staged: boolean): string | null {
  if (!seed || staged) return null;
  return seed;
}

export default function ChatView({
  repo,
  seed,
  onSeedUsed,
  initialRuns,
}: {
  repo: string;
  seed: string | null;
  onSeedUsed: () => void;
  // Test seam (drawer-precedented): pins the composer-above-runs
  // order without waiting on the runs fetch.
  initialRuns?: AgentRun[];
}) {
  // Restored live exchange (#573): the initializer replays the
  // sessionStorage snapshot, so a tab switch (unmount/remount) keeps
  // the conversation. Mock mode starts empty; its effect below loads
  // the canned fixture instead.
  const [messages, setMessages] = useState<ChatMessage[]>(() =>
    useMock() ? [] : loadChatMessages(),
  );
  // Restored composer draft (#402): an explicit seed wins, else the
  // sessionStorage snapshot replays so a tab switch keeps the draft.
  // Mock mode starts empty, matching the message snapshot.
  const [prompt, setPrompt] = useState(
    seed ?? (useMock() ? '' : loadChatDraft()),
  );
  const promptRef = useRef<HTMLTextAreaElement>(null);
  // A seed stages into the composer for review and edit — the turn
  // starts only when the user taps Send (#507). The ref survives
  // StrictMode's double effect-invoke so dev never double-stages.
  const seedStagedRef = useRef(false);
  useEffect(() => {
    const staged = stageSeed(seed, seedStagedRef.current);
    if (staged === null) return;
    seedStagedRef.current = true;
    setPrompt(staged);
    onSeedUsed();
    promptRef.current?.focus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const [streaming, setStreaming] = useState(false);
  const [phase, setPhase] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastSent, setLastSent] = useState('');
  const [runs, setRuns] = useState<AgentRun[]>(initialRuns ?? []);
  const [budget, setBudget] = useState<BudgetStatus | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<AgentRunDetail | null>(null);
  const [runsNotice, setRunsNotice] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  // @-mention completion (#653): caret-tracked query over the
  // repo-files endpoint; accepting inserts "@path" chip text.
  const [caret, setCaret] = useState<number | null>(null);
  const [mentionFiles, setMentionFiles] = useState<string[]>([]);
  const [mentionTruncated, setMentionTruncated] = useState(false);
  const [mentionActive, setMentionActive] = useState(0);
  const [mentionDismissed, setMentionDismissed] = useState<string | null>(null);
  const pendingCaretRef = useRef<number | null>(null);
  const mention = caret === null ? null : mentionQueryAt(prompt, caret);
  const mentionOpen =
    mention !== null &&
    !streaming &&
    mentionFiles.length > 0 &&
    mentionDismissed !== mention.query;
  // Slash-command menu (#669): leading-"/" token over SLASH_COMMANDS.
  // Unknown tokens match nothing, so the menu stays shut and the
  // text sends normally. Mirrors the mention dismissed/active pattern.
  const [slashActive, setSlashActive] = useState(0);
  const [slashDismissed, setSlashDismissed] = useState<string | null>(null);
  const backendGroupRef = useRef<HTMLDivElement>(null);
  const slashQuery = slashQueryAt(prompt);
  const slashMatches = slashQuery === null ? [] : matchSlashCommands(slashQuery);
  const slashOpen =
    slashQuery !== null &&
    !streaming &&
    slashMatches.length > 0 &&
    slashDismissed !== slashQuery;
  // Saved prompt templates (#807): seeds on first run, user store
  // after. Mock mode keeps seeds without touching localStorage.
  const [templates, setTemplates] = useState<PromptTemplate[]>(() =>
    useMock() ? [...SEED_TEMPLATES] : loadPromptTemplates(),
  );
  const [templatesOpen, setTemplatesOpen] = useState(false);
  // Voice dictation (#659): tap-to-dictate session state. The live
  // recognition object sits in a ref (mute, event-driven — never
  // state); inserted tracks this session's preview suffix for
  // applyDictationResult rebasing.
  const [dictating, setDictating] = useState(false);
  const [dictationHint, setDictationHint] = useState<string | null>(null);
  const dictationRecRef = useRef<DictationRecognition | null>(null);
  const dictationInsertedRef = useRef('');
  const [approval, setApproval] = useState<ApprovalPayload | null>(null);
  const [decided, setDecided] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [tokenCount, setTokenCount] = useState(0);
  // Composer backend picker: per-turn override for the next reply.
  // View-level state, so the choice persists across sends for this
  // view session. Auto = today's ladder, untouched.
  const [backend, setBackend] = useState<ChatBackend>('auto');
  // Pre-send route hint for the composer line. Refetched whenever
  // the picker changes; cleared first so a stale backend never
  // shows under a new selection. Mock mode uses a canned local
  // hint (no network, deterministic ?mock=1).
  const [hint, setHint] = useState<RouteHintResponse | null>(null);
  // Conversation thread id (#467): one id spans the whole visible
  // conversation; aborts reuse it because only one turn streams at a
  // time. New chat resets it (see onClearChat).
  const sessionRef = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    if (useMock()) {
      setHint({
        ok: true,
        provider: 'ollama',
        model: 'qwen3:8b',
        metered: false,
        rate_in_per_1m: null,
        rate_out_per_1m: null,
        error: '',
      });
      return;
    }
    setHint(null);
    fetchRouteHint(backend).then(
      (h) => {
        if (!cancelled) setHint(h);
      },
      () => {
        if (!cancelled) {
          setHint({
            ok: false,
            provider: '',
            model: '',
            metered: false,
            rate_in_per_1m: null,
            rate_out_per_1m: null,
            error: 'hint unavailable',
          });
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [backend]);
  // @-mention completions: refetch as the query changes; a fresh
  // query re-arms after Escape. Mock mode stays offline (no
  // dropdown in ?mock=1).
  const mentionQuery = mention?.query;
  useEffect(() => {
    setMentionDismissed(null);
    if (mentionQuery === undefined || useMock()) {
      setMentionFiles([]);
      setMentionTruncated(false);
      return;
    }
    let cancelled = false;
    setMentionActive(0);
    fetchRepoFiles(repo, mentionQuery).then(
      (res) => {
        if (cancelled) return;
        setMentionFiles(res.files);
        setMentionTruncated(res.truncated);
      },
      () => {
        if (cancelled) return;
        setMentionFiles([]);
        setMentionTruncated(false);
      },
    );
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mentionQuery, repo]);
  // After a mention accept, land the caret past the inserted chip.
  useEffect(() => {
    if (pendingCaretRef.current === null) return;
    const pos = pendingCaretRef.current;
    pendingCaretRef.current = null;
    promptRef.current?.setSelectionRange(pos, pos);
    setCaret(pos);
  });
  // A fresh slash token re-arms the menu after Escape and resets
  // the highlight, mirroring the mention-query effect.
  useEffect(() => {
    setSlashDismissed(null);
    setSlashActive(0);
  }, [slashQuery]);
  // Leaving the view mid-dictation stops the mic: no orphan session
  // keeps appending into an unmounted composer.
  useEffect(() => {
    return () => {
      const rec = dictationRecRef.current;
      dictationRecRef.current = null;
      if (rec) {
        try {
          rec.stop();
        } catch {
          // Already ended.
        }
      }
    };
  }, []);
  const abortRef = useRef<AbortController | null>(null);
  const startRef = useRef(0);
  const lastEventRef = useRef(0);
  const stalledRef = useRef(false);

  useEffect(() => {
    if (useMock()) {
      setMessages(loadChat().messages);
      return;
    }
    (async () => {
      try {
        const live = await fetchAgentRuns();
        setRuns(live.runs);
      } catch {
        // Past runs stay hidden; live chat still works.
      }
      try {
        setBudget(await fetchBudget());
      } catch {
        // Budget line stays hidden; runs still render.
      }
    })();
  }, []);

  // Persist the live exchange across tab switches (#573): the view
  // unmounts whenever the tab changes, so every settled change lands
  // in sessionStorage for the initializer above. Clearing the chat
  // persists the empty list, which resets the store.
  useEffect(() => {
    if (useMock()) return;
    saveChatMessages(settledChatMessages(messages));
  }, [messages]);

  // Persist the composer draft across tab switches (#402): every
  // change lands in sessionStorage for the initializer above. An
  // empty composer removes the snapshot, so sending (which clears
  // the prompt) resets the store along with the composer.
  useEffect(() => {
    if (useMock()) return;
    if (prompt === '') clearChatDraft();
    else saveChatDraft(prompt);
  }, [prompt]);

  async function onSelectRun(id: string) {
    setSelected(id);
    setDetail(null);
    try {
      setDetail(await fetchAgentRun(id));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function refreshRuns() {
    try {
      setRuns((await fetchAgentRuns()).runs);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function onContinueRun(id: string) {
    try {
      const d = selected === id && detail ? detail : await fetchAgentRun(id);
      const extra = d.entry.extra as Record<string, unknown> | undefined;
      const stored = str(extra?.prompt);
      if (!stored) {
        setRunsNotice('That run has no stored prompt to continue from.');
        return;
      }
      setPrompt(stored);
      setRunsNotice(null);
      promptRef.current?.focus();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function onDeleteRun(id: string) {
    try {
      await deleteAgentRun(id);
      if (selected === id) {
        setSelected(null);
        setDetail(null);
      }
      await refreshRuns();
      setRunsNotice(`Deleted ${id}.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function onClearFinished() {
    try {
      const result = await clearAgentRuns();
      setSelected(null);
      setDetail(null);
      await refreshRuns();
      setRunsNotice(`Cleared ${result.cleared} finished run(s).`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  function appendToken(token: string) {
    if (!token) return;
    setTokenCount((c) => c + 1);
    setMessages((m) => {
      const next = [...m];
      const last = next[next.length - 1];
      next[next.length - 1] = { role: 'assistant', text: last.text + token };
      return next;
    });
  }

  function backfillDone(text: string) {
    if (!text) return;
    setMessages((m) => {
      const next = [...m];
      const last = next[next.length - 1];
      if (!last || last.role !== 'assistant' || last.text.length > 0) return m;
      next[next.length - 1] = { role: 'assistant', text };
      return next;
    });
  }

  async function streamLive(text: string, sessionId: string) {
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const resp = await postChatStream(text, repo, sessionId, backend, ctrl.signal);
      if (!resp.ok || !resp.body) throw new Error(`Chat stream failed: ${resp.status}`);
      await pumpStream(
        resp.body.getReader(),
        appendToken,
        (payload) => {
          setApproval(payload);
          setDecided(null);
        },
        setPhase,
        backfillDone,
        () => {
          lastEventRef.current = Date.now();
        },
      );
    } catch (err) {
      if ((err as Error).name !== 'AbortError') throw err;
    } finally {
      abortRef.current = null;
    }
  }

  function mockReply() {
    const text = loadChat().messages.find((x) => x.role === 'assistant')?.text ?? 'Mock reply.';
    setMessages((m) => [...m, { role: 'assistant', text }]);
  }

  async function sendText(text: string) {
    setError(null);
    setPhase(null);
    setApproval(null);
    setDecided(null);
    setLastSent(text);
    // Reuse the conversation thread (#467): the stored snapshot
    // covers tab-switch remounts; mock mode skips storage (pure mint).
    const sessionId = resolveChatSessionId(
      sessionRef.current,
      useMock() ? null : loadChatSessionId(),
      newSessionId,
    );
    sessionRef.current = sessionId;
    if (!useMock()) saveChatSessionId(sessionId);
    setMessages((m) => [...m, { role: 'user', text }]);
    setPrompt('');
    if (useMock()) {
      mockReply();
      return;
    }
    startRef.current = Date.now();
    lastEventRef.current = Date.now();
    stalledRef.current = false;
    setElapsed(0);
    setTokenCount(0);
    setStreaming(true);
    setMessages((m) => [...m, { role: 'assistant', text: '' }]);
    try {
      await streamLive(text, sessionId);
      await refreshRunsQuiet(fetchAgentRuns, setRuns);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setStreaming(false);
    }
  }

  async function onSend(e?: React.FormEvent) {
    e?.preventDefault();
    const text = prompt.trim();
    if (!text || streaming) return;
    await sendText(text);
  }

  function onRetry() {
    if (!lastSent.trim() || streaming) return;
    // Reuse the pending exchange: strip the failed pair first so
    // the resend below appends exactly one fresh pair. Functional
    // updates queue in order, so the strip lands before the appends.
    setMessages((m) => stripFailedRetryExchange(m, lastSent));
    void sendText(lastSent);
  }

  function onPickChip(text: string, altKey: boolean) {
    const action = chipPickAction(text, altKey, streaming);
    if (action === 'fill') {
      setPrompt(text);
      promptRef.current?.focus({ preventScroll: true });
    } else if (action === 'send') {
      void sendText(text);
    }
  }

  // New-chat reset (#669): drops the visible conversation and its
  // pending state. Past runs on the backend are untouched. The
  // conversation thread resets too (#467), so the next send mints a
  // fresh session id: a new conversation explicitly starts a new
  // thread.
  function onClearChat() {
    if (streaming) return;
    setMessages([]);
    setPrompt('');
    setError(null);
    setLastSent('');
    setApproval(null);
    setDecided(null);
    setPhase(null);
    sessionRef.current = null;
    if (!useMock()) clearChatSessionId();
    promptRef.current?.focus();
  }

  function onExportTranscript() {
    downloadTranscript(messages);
  }

  // Slash pick (#669): the token is always cleared first, then the
  // table entry runs its UI action. /attach seeds a bare "@" so the
  // mention dropdown opens; /model lands focus on the picker group.
  function onPickSlash(cmd: string) {
    const found = SLASH_COMMANDS.find((c) => c.cmd === cmd);
    if (!found) return;
    setPrompt('');
    found.run({
      retry: () => onRetry(),
      clear: () => onClearChat(),
      focusModel: () => backendGroupRef.current?.focus(),
      attach: () => {
        if (streaming) return;
        setPrompt('@');
        pendingCaretRef.current = 1;
        promptRef.current?.focus();
      },
      exportTranscript: () => onExportTranscript(),
    });
  }

  // Template save/insert/delete (#807): store mutations persist
  // (outside mock mode); inserts reuse the pending-caret effect so
  // the caret lands at the slot or past the insert.
  function onSaveTemplate() {
    if (streaming) return;
    const made = makeTemplateFromDraft(prompt);
    if (!made) return;
    const next = [...templates, made];
    setTemplates(next);
    if (!useMock()) savePromptTemplates(next);
    promptRef.current?.focus();
  }

  function onInsertTemplate(text: string) {
    if (streaming) return;
    const next = insertTemplateAt(prompt, caret ?? prompt.length, text);
    pendingCaretRef.current = next.caret;
    setPrompt(next.text);
    promptRef.current?.focus();
  }

  function onDeleteTemplate(id: string) {
    if (streaming) return;
    const next = templates.filter((t) => t.id !== id);
    setTemplates(next);
    if (!useMock()) savePromptTemplates(next);
  }

  // @-mention accept: swap the "@query" token for "@path" chip
  // text; the caret lands past the trailing space via the
  // pending-caret effect, which also closes the dropdown.
  function onAcceptMention(path: string) {
    if (caret === null) return;
    const next = insertMention(prompt, caret, path);
    if (!next) return;
    pendingCaretRef.current = next.cursor;
    setPrompt(next.text);
  }

  // Edit recalls a sent prompt into the composer for fix-and-resend;
  // the resend is a normal new turn (history stays append-only).
  // preventScroll keeps the recalled turn in view on phones.
  function onEditPrompt(text: string) {
    if (streaming) return;
    setPrompt(text);
    promptRef.current?.focus({ preventScroll: true });
  }

  // Regenerate re-sends a completed turn's prompt through the same
  // path as failure-retry — manual only, never mid-stream.
  function onRegenerate(text: string) {
    if (!text.trim() || streaming) return;
    void sendText(text);
  }

  // Tap toggles a recognition session: interim results stream into
  // the composer live, finals commit; tap again (or silence
  // auto-end — continuous is false) stops. Mic permission is
  // browser-mediated per tap; there is no app permission flow.
  function onToggleDictation() {
    const active = dictationRecRef.current;
    if (active) {
      try {
        active.stop();
      } catch {
        // Already ended; onend still resets the state below.
      }
      return;
    }
    const Ctor = dictationCtor();
    if (!Ctor) return;
    const rec = new Ctor();
    rec.lang =
      typeof navigator !== 'undefined' && navigator.language
        ? navigator.language
        : 'en-US';
    rec.interimResults = true;
    rec.continuous = false;
    dictationInsertedRef.current = '';
    rec.onresult = (ev) => {
      const { finalText, interimText } = extractDictationText(ev);
      setPrompt((prev) => {
        const applied = applyDictationResult(
          prev,
          dictationInsertedRef.current,
          finalText,
          interimText,
        );
        dictationInsertedRef.current = applied.inserted;
        return applied.next;
      });
      setCaret(null);
    };
    rec.onerror = (ev) => {
      const hintText = dictationErrorHint(ev.error);
      if (hintText) setDictationHint(hintText);
    };
    rec.onend = () => {
      dictationRecRef.current = null;
      dictationInsertedRef.current = '';
      setDictating(false);
    };
    dictationRecRef.current = rec;
    setDictationHint(null);
    try {
      rec.start();
    } catch {
      dictationRecRef.current = null;
      return;
    }
    setDictating(true);
  }

  function stopStream() {
    abortRef.current?.abort();
    abortRef.current = null;
    setStreaming(false);
    setPhase(null);
  }

  async function onAbort() {
    stopStream();
    const sid = sessionRef.current;
    if (!sid || useMock()) return;
    try {
      await abortChat(sid);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  function onStall() {
    stopStream();
    const sid = sessionRef.current;
    setError(`Stream stalled — no response for ${STALL_SECONDS}s.`);
    if (!sid || useMock()) return;
    void abortChat(sid).catch(() => {
      // Run already dead; the stall message stands.
    });
  }

  // Elapsed ticker + stall watchdog while streaming.
  useEffect(() => {
    if (!streaming) return;
    const t = window.setInterval(() => {
      setElapsed(Math.floor((Date.now() - startRef.current) / 1000));
      if (!stalledRef.current && isStalled(lastEventRef.current, Date.now())) {
        stalledRef.current = true;
        onStall();
      }
    }, 1000);
    return () => window.clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [streaming]);

  async function onDecide(decision: 'approve' | 'reject') {
    if (!approval || decided) return;
    setDecided(decision === 'approve' ? 'Approved — running…' : 'Rejected.');
    try {
      const result = await resumeChat(approval.session_id, decision);
      setMessages((m) => [...m, { role: 'assistant', text: result.text }]);
      if (result.approval) {
        setApproval(result.approval);
        setDecided(null);
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      if (/: 409\b/.test(msg)) {
        setDecided('Already decided.');
      } else {
        setError(msg);
        setDecided(null);
      }
    }
  }

  // Pin the live reply in view while streaming: the reply renders
  // at the list end and used to demand a manual scroll back up past
  // the runs after every send (#594). Nearest-scroll only moves when
  // the tail is actually out of view.
  useEffect(() => {
    if (!streaming) return;
    bottomRef.current?.scrollIntoView({ block: 'nearest' });
  }, [streaming, messages]);

  return (
    <section data-testid="chat-view">
      <header className="page-head">
        <div>
          <h2>Chat</h2>
          <RepoLabel repo={repo} />
        </div>
        {streaming && (
          <button type="button" data-testid="chat-abort" onClick={() => void onAbort()}>
            Abort
          </button>
        )}
      </header>
      {error && (
        <p className="row-meta" data-testid="chat-error" role="alert">
          Chat failed: {error}
        </p>
      )}
      {shouldShowChatRetry(error, streaming, lastSent) && (
        <button type="button" data-testid="chat-retry" onClick={() => onRetry()}>
          Retry
        </button>
      )}
      {streaming && <StreamProgress elapsed={elapsed} phase={phase} tokens={tokenCount} />}
      <span className="sr-only" role="status" data-testid="chat-phase">
        {streaming ? phase : null}
      </span>
      <ChatHistory
        messages={messages}
        streaming={streaming}
        onEdit={(text) => onEditPrompt(text)}
        onRegenerate={(text) => onRegenerate(text)}
      />
      <div ref={bottomRef} data-testid="chat-bottom" aria-hidden="true" />
      {messages.length === 0 && (
        <p className="row-meta" data-testid="chat-empty">
          Ask anything about this repo.
        </p>
      )}
      {approval && (
        <ApprovalCard approval={approval} decided={decided} onDecide={(d) => void onDecide(d)} />
      )}
      {/* Composer above Past runs: with 8+ runs the form used to
          drop below every run card, burying Prompt + Send (#564). */}
      <PromptChips disabled={streaming} onPick={(text, altKey) => onPickChip(text, altKey)} />
      <form onSubmit={(e) => void onSend(e)}>
        <label className="row-meta" htmlFor="chat-prompt">
          Prompt{' '}
          <textarea
            id="chat-prompt"
            data-testid="chat-prompt"
            ref={promptRef}
            value={prompt}
            onChange={(e) => {
              setPrompt(e.target.value);
              setCaret(e.target.selectionStart);
            }}
            onSelect={(e) => setCaret(e.currentTarget.selectionStart)}
            onKeyDown={(e) => {
              if (mentionOpen && e.key === 'ArrowDown') {
                e.preventDefault();
                setMentionActive((a) => (a + 1) % mentionFiles.length);
                return;
              }
              if (mentionOpen && e.key === 'ArrowUp') {
                e.preventDefault();
                setMentionActive(
                  (a) => (a - 1 + mentionFiles.length) % mentionFiles.length,
                );
                return;
              }
              if (
                mentionOpen &&
                (e.key === 'Enter' || e.key === 'Tab') &&
                !(e.ctrlKey || e.metaKey)
              ) {
                e.preventDefault();
                onAcceptMention(
                  mentionFiles[mentionActive] ?? mentionFiles[0] ?? '',
                );
                return;
              }
              if (mentionOpen && e.key === 'Escape') {
                e.preventDefault();
                setMentionDismissed(mention?.query ?? '');
                return;
              }
              if (slashOpen && e.key === 'ArrowDown') {
                e.preventDefault();
                setSlashActive((a) => (a + 1) % slashMatches.length);
                return;
              }
              if (slashOpen && e.key === 'ArrowUp') {
                e.preventDefault();
                setSlashActive(
                  (a) => (a - 1 + slashMatches.length) % slashMatches.length,
                );
                return;
              }
              if (
                slashOpen &&
                (e.key === 'Enter' || e.key === 'Tab') &&
                !(e.ctrlKey || e.metaKey)
              ) {
                e.preventDefault();
                onPickSlash(
                  slashMatches[slashActive]?.cmd ?? slashMatches[0]?.cmd ?? '',
                );
                return;
              }
              if (slashOpen && e.key === 'Escape') {
                e.preventDefault();
                setSlashDismissed(slashQuery ?? '');
                return;
              }
              if (isKeyboardSend(e)) {
                e.preventDefault();
                void onSend();
              }
            }}
            disabled={streaming}
          />
        </label>
        {mentionOpen && (
          <MentionDropdown
            files={mentionFiles}
            truncated={mentionTruncated}
            active={mentionActive}
            onPick={(path) => onAcceptMention(path)}
            onHover={(i) => setMentionActive(i)}
          />
        )}
        {slashOpen && (
          <SlashMenu
            commands={slashMatches}
            active={slashActive}
            onPick={(cmd) => onPickSlash(cmd)}
            onHover={(i) => setSlashActive(i)}
          />
        )}
        {templatesOpen && (
          <TemplatePanel
            templates={templates}
            canSave={prompt.trim().length > 0}
            disabled={streaming}
            onSave={() => onSaveTemplate()}
            onInsert={(text) => onInsertTemplate(text)}
            onDelete={(id) => onDeleteTemplate(id)}
          />
        )}
        <p className="row-meta" data-testid="chat-route-hint" role="status">
          {hint ? hintLine(hint) : 'Next reply via …'}
        </p>
        {dictationHint && <DictationHint text={dictationHint} />}
        <div
          className="actions"
          role="group"
          aria-label="Chat backend"
          data-testid="chat-backend-picker"
          ref={backendGroupRef}
          tabIndex={-1}
        >
          {CHAT_BACKEND_OPTIONS.map((o) => (
            <button
              key={o.value}
              type="button"
              data-testid={`chat-backend-${o.value}`}
              aria-pressed={backend === o.value}
              disabled={streaming}
              onClick={() => setBackend(o.value)}
            >
              {o.label}
            </button>
          ))}
        </div>
        <div className="actions">
          <button
            type="submit"
            data-testid="chat-send"
            title="Send (Ctrl+Enter or Cmd+Enter)"
            disabled={streaming || !prompt.trim()}
          >
            Send
          </button>
          <DictateButton
            recording={dictating}
            disabled={streaming}
            onToggle={() => onToggleDictation()}
          />
          <button
            type="button"
            data-testid="chat-clear"
            title="Clear this conversation (or type /clear)"
            disabled={streaming}
            onClick={() => onClearChat()}
          >
            New chat
          </button>
          <button
            type="button"
            data-testid="chat-export"
            title="Download transcript as Markdown (or type /export)"
            disabled={messages.length === 0}
            onClick={() => onExportTranscript()}
          >
            Export
          </button>
          <button
            type="button"
            data-testid="chat-templates"
            title="Saved prompt templates"
            aria-expanded={templatesOpen}
            onClick={() => setTemplatesOpen((o) => !o)}
          >
            Templates
          </button>
        </div>
      </form>
      <RunsPane
        runs={runs}
        selected={selected}
        detail={detail}
        onSelect={(id) => void onSelectRun(id)}
        onContinue={(id) => void onContinueRun(id)}
        onDelete={(id) => void onDeleteRun(id)}
        onClearFinished={() => void onClearFinished()}
        notice={runsNotice}
        budget={budget}
      />
      <AgentDrawer repo={repo} />
    </section>
  );
}
