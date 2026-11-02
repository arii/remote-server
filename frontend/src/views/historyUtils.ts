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

export interface HistoryRow {
  key: string;
  kind: string;
  summary: string;
  at: string;
  repeat: number;
  recovery: string;
}

interface FlatEntry {
  kind: string;
  summary: string;
  at: string;
  rawAt: number;
  failed: boolean;
  routine: boolean;
}

// Routine lifecycle chatter (chat start/finish/abort): true but noisy.
// Consecutive runs collapse into one "activity" row (see toHistoryRows).
const ROUTINE_RE = /^chat (started|finished|aborted)/i;

function isFailure(kind: string, raw: string, summary: string): boolean {
  if (/fail|error|refus|reject|conflict|not permitted|aborted/i.test(raw)) return true;
  if (/fail|error|refused|rejected|conflict|not permitted|aborted/i.test(summary)) return true;
  return kind === 'pr-action' && /failed/i.test(summary);
}

function recoveryFor(kind: string, summary: string): string {
  const s = `${kind} ${summary}`.toLowerCase();
  if (s.includes('fast-forward') || s.includes('remote has new'))
    return 'How to recover: pull, resolve, push';
  if (s.includes('browser') || s.includes('playwright'))
    return 'How to recover: reinstall browsers and rerun';
  if (s.includes('network') || s.includes('could not be reached'))
    return 'How to recover: retry when the service is reachable';
  if (s.includes('not permitted') || s.includes('access'))
    return 'How to recover: check access and retry';
  if (s.includes('conflict'))
    return 'How to recover: review and resolve conflicts';
  return 'How to recover: check status and retry';
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
  return {
    kind: j.kind || 'job',
    summary,
    at: status,
    rawAt: jobTime(j),
    failed: /fail|error/i.test(status),
    routine: false,
  };
}

function ledgerToEntry(e: LedgerEntry): FlatEntry {
  const raw = e.event ?? e.action ?? e.kind;
  const summary = plainLanguage(raw ?? '', e.kind);
  const failed =
    e.ok === false ||
    (e.status !== undefined && /fail|error/i.test(String(e.status))) ||
    isFailure(e.kind, raw ?? '', summary);
  return {
    kind: e.kind,
    summary,
    at: rowTime(e.ts ?? e.at),
    rawAt: epochOf(e.ts ?? e.at),
    failed,
    routine: ROUTINE_RE.test(summary),
  };
}

function sameFailure(a: FlatEntry, b: FlatEntry): boolean {
  return a.kind === b.kind && a.summary === b.summary;
}

function toRow(entry: FlatEntry, count: number, index: number): HistoryRow {
  const routineRun = entry.routine && count > 1;
  return {
    key: `${entry.kind}-${entry.summary}-${index}`,
    kind: entry.kind,
    summary: routineRun
      ? `Chat session activity ×${count}`
      : count > 1
        ? `${entry.summary} ×${count} — same failure repeated`
        : entry.summary,
    at: entry.at,
    repeat: count,
    recovery: entry.failed && !entry.routine ? recoveryFor(entry.kind, entry.summary) : '',
  };
}

/**
 * Merge jobs + ledger entries into display rows, newest first.
 *
 * Incident-collapse merges CONSECUTIVE equal failures only: rows with the
 * same kind + summary that sit next to each other in newest-first order
 * fold into one "×N" row. Equal failures separated by other rows stay
 * separate, so the count reflects one continuous incident, not a total.
 * Consecutive routine lifecycle rows (chat started/finished/aborted) fold
 * into one "Chat session activity ×N" row regardless of exact mix.
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
        (flat[i].routine && flat[i + count].routine))
    ) {
      count++;
    }
    rows.push(toRow(flat[i], count, i));
    i += count;
  }
  return rows;
}
