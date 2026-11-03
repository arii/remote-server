import type { JobRecord, LedgerEntry } from '../apiTypes';

// Plain-language summarizer. Never surfaces raw stderr/log lines: known
// noisy patterns map to short human sentences; anything else falls back
// to a generic, non-technical label plus the kind.
const RULES: Array<[RegExp, string]> = [
  [/fast-?forward/i, 'Pull could not fast-forward — the remote has new commits'],
  [/fetch.?first|non-fast/i, 'Push was rejected — the remote has new commits'],
  [/connection refused|econnrefused|network unreachable|timeout/i, 'Network request failed — the service could not be reached'],
  [/playwright|chromium executable missing|browser.*crash/i, 'Automated browser test could not start — browser is missing or crashed'],
  [/annotation|check-run/i, 'Automated check reported a failure'],
  [/conflict/i, 'Changes conflict — manual review is needed'],
  [/permission|forbidden|unauthorized|401|403/i, 'Action was not permitted — check access'],
];

export function plainLanguage(raw: string, fallbackKind: string): string {
  const text = (raw ?? '').trim();
  for (const [re, label] of RULES) {
    if (re.test(text)) return label;
  }
  if (!text) return `${fallbackKind || 'Event'} — no details recorded`;
  const first = text.split('\n')[0].slice(0, 120);
  if (/error|fail|exception|stderr|at\s+\S+\(|traceback/i.test(first)) {
    return `${fallbackKind || 'Event'} failed — see detail trace`;
  }
  return first;
}

export type HistoryTone = 'bad' | 'ok' | 'muted';

// One collapsed group member: its own time, full detail block, and the
// run id when it came from a job record (for lazy run-output fetch).
export interface HistoryMember {
  at: string;
  detail: string;
  runId?: string;
}

export interface HistoryRow {
  key: string;
  kind: string;
  summary: string;
  at: string;
  repeat: number;
  recovery: string;
  tone: HistoryTone;
  detail: string;
  members: HistoryMember[];
}

interface FlatEntry {
  kind: string;
  summary: string;
  at: string;
  rawAt: number;
  failed: boolean;
  routine: boolean;
  detail: string;
  runId?: string;
}

// Routine lifecycle chatter (chat start/finish/abort, job step progress):
// true but noisy. Consecutive same-kind runs collapse into one
// "activity" row (see toHistoryRows).
const ROUTINE_RE =
  /^(chat (started|finished|aborted)|step (auto-?done|done|started)|created|job$)/i;

function isFailure(kind: string, raw: string, summary: string): boolean {
  if (/fail|error|refus|reject|conflict|not permitted|aborted/i.test(raw)) return true;
  if (/fail|error|refused|rejected|conflict|not permitted|aborted/i.test(summary)) return true;
  return kind === 'pr-action' && /failed/i.test(summary);
}

const RECOVERY_RULES: Array<[RegExp, string]> = [
  [/fast-forward|remote has new/, 'pull, resolve, push'],
  [/browser|playwright/, 'reinstall browsers and rerun'],
  [/network|could not be reached/, 'retry when the service is reachable'],
  [/not permitted|check access/, 'check access and retry'],
  [/conflict/, 'review and resolve conflicts'],
];

function recoveryFor(kind: string, summary: string): string {
  const s = `${kind} ${summary}`.toLowerCase();
  for (const [re, label] of RECOVERY_RULES) {
    if (re.test(s)) return label;
  }
  return 'check status and retry';
}

function rowTime(at: number | string | undefined): string {
  if (at === undefined || at === null || at === '') return 'unknown time';
  const n = typeof at === 'string' ? Date.parse(at) : at;
  if (Number.isNaN(n)) return String(at);
  const ms = n < 1e12 ? n * 1000 : n;
  return new Date(ms).toLocaleString();
}

function epochOf(at: number | string | undefined): number {
  if (typeof at === 'number') return at < 1e12 ? at * 1000 : at;
  if (typeof at === 'string' && at !== '') {
    const parsed = Date.parse(at);
    if (!Number.isNaN(parsed)) return parsed;
  }
  return 0;
}

function jobStatus(j: JobRecord): string {
  if (j.status) return j.status;
  if (j.state) return j.state;
  return j.done ? 'done' : 'pending';
}

function jobTitle(j: JobRecord): string {
  return j.title ?? j.name ?? j.id;
}

function jobTime(j: JobRecord): number {
  return epochOf(j.at ?? j.created_at);
}

function jobToEntry(j: JobRecord): FlatEntry {
  const status = jobStatus(j);
  const summary = plainLanguage(`${jobTitle(j)}: ${status}`, 'Job');
  const lines = [`run: ${j.id}`, `kind: ${j.kind || 'job'}`, `status: ${status}`];
  if (j.repo) lines.push(`repo: ${j.repo}`);
  if (typeof j.exit_code === 'number') lines.push(`exit: ${j.exit_code}`);
  if (j.error) lines.push(`error: ${j.error}`.slice(0, 1500));
  return {
    kind: j.kind || 'job',
    summary,
    at: rowTime(j.at ?? j.created_at),
    rawAt: jobTime(j),
    failed: /fail|error/i.test(status),
    routine: false,
    detail: lines.join('\n'),
    runId: j.id,
  };
}

function entryFailed(e: LedgerEntry, raw: string, summary: string): boolean {
  if (e.ok === false) return true;
  if (e.status !== undefined && /fail|error/i.test(String(e.status))) return true;
  return isFailure(e.kind, raw, summary);
}

function ledgerToEntry(e: LedgerEntry): FlatEntry {
  const raw = e.event ?? e.action ?? e.kind;
  // Backend-supplied reasons are already human sentences; use verbatim.
  const summary =
    e.error && e.error.trim()
      ? e.error.trim().slice(0, 200)
      : plainLanguage(raw ?? '', e.kind);
  const lines = [`event: ${raw ?? e.kind}`];
  if (e.error && e.error.trim()) lines.push(`error: ${e.error.trim()}`.slice(0, 1500));
  if (e.detail) lines.push(`detail: ${e.detail}`.slice(0, 1500));
  if (e.repo) lines.push(`repo: ${e.repo}`);
  if (e.session_id) lines.push(`session: ${e.session_id}`);
  if (e.prompt) lines.push(`prompt: ${e.prompt}`.slice(0, 500));
  if (e.command) lines.push(`command: ${e.command}`.slice(0, 1500));
  if (typeof e.exit === 'number') lines.push(`exit: ${e.exit}`);
  const elapsed = e.elapsed ?? e.elapsed_s;
  if (typeof elapsed === 'number') lines.push(`elapsed: ${elapsed}s`);
  return {
    kind: e.kind,
    summary,
    at: rowTime(e.ts ?? e.at),
    rawAt: epochOf(e.ts ?? e.at),
    failed: entryFailed(e, raw ?? '', summary),
    routine: ROUTINE_RE.test(summary),
    detail: lines.join('\n').slice(0, 2000),
  };
}

function sameFailure(a: FlatEntry, b: FlatEntry): boolean {
  return a.kind === b.kind && a.summary === b.summary;
}

function activityLabel(kind: string, count: number): string {
  const what = kind === 'chat' ? 'Chat session' : kind === 'job' ? 'Job' : 'Routine';
  return `${what} activity ×${count}`;
}

export function jobPassesKindFilter(jobKind: string | undefined, filter: string): boolean {
  if (filter === 'all') return true;
  // Job rows are runs: they answer to the run filter whatever action ran.
  if (filter === 'run') return true;
  return (jobKind || 'job') === filter;
}

// Keep run-output disclosures readable: show the last maxLines lines and
// say how many earlier lines were hidden. Empty output stays empty.
export function tailText(output: string, maxLines = 40): string {
  const text = (output ?? '').replace(/\n$/, '');
  if (!text) return '';
  const lines = text.split('\n');
  if (lines.length <= maxLines) return text;
  const hidden = lines.length - maxLines;
  return `… (${hidden} earlier line${hidden === 1 ? '' : 's'} hidden)\n${lines.slice(-maxLines).join('\n')}`;
}

function toRow(entry: FlatEntry, members: FlatEntry[], index: number): HistoryRow {
  const count = members.length;
  const routineRun = entry.routine && count > 1;
  const failed = entry.failed && !entry.routine;
  const repeated = count > 1 && !routineRun
    ? failed
      ? `${entry.summary} ×${count} — same failure repeated`
      : `${entry.summary} ×${count}`
    : null;
  return {
    key: `${entry.kind}-${entry.summary}-${index}`,
    kind: entry.kind,
    summary: routineRun ? activityLabel(entry.kind, count) : (repeated ?? entry.summary),
    at: entry.at,
    repeat: count,
    recovery: failed ? recoveryFor(entry.kind, entry.summary) : '',
    tone: failed ? 'bad' : entry.routine ? 'muted' : 'ok',
    detail: entry.detail,
    members: members.map((m) => ({
      at: m.at,
      detail: m.detail,
      ...(m.runId ? { runId: m.runId } : {}),
    })),
  };
}

/**
 * Merge jobs + ledger entries into display rows, newest first.
 *
 * Incident-collapse merges CONSECUTIVE equal failures only: rows with the
 * same kind + summary that sit next to each other in newest-first order
 * fold into one "×N" row. Equal failures separated by other rows stay
 * separate, so the count reflects one continuous incident, not a total.
 * Consecutive routine lifecycle rows of the same kind fold into one
 * "<Kind> activity ×N" row regardless of exact mix.
 */
export function toHistoryRows(
  jobs: JobRecord[],
  entries: LedgerEntry[],
): HistoryRow[] {
  const flat: FlatEntry[] = [
    ...jobs.map(jobToEntry),
    ...entries.map(ledgerToEntry),
  ];
  flat.sort((a, b) => b.rawAt - a.rawAt);
  const rows: HistoryRow[] = [];
  let i = 0;
  while (i < flat.length) {
    let count = 1;
    while (
      i + count < flat.length &&
      (sameFailure(flat[i + count], flat[i]) ||
        (flat[i].routine &&
          flat[i + count].routine &&
          flat[i + count].kind === flat[i].kind))
    ) {
      count++;
    }
    rows.push(toRow(flat[i], flat.slice(i, i + count), i));
    i += count;
  }
  return rows;
}
