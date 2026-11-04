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

type HistoryTone = 'bad' | 'ok' | 'muted';

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

export function rowTime(at: number | string | undefined): string {
  if (at === undefined || at === null || at === '') return 'unknown time';
  const n = typeof at === 'string' ? Date.parse(at) : at;
  if (Number.isNaN(n)) return String(at);
  const ms = n < 1e12 ? n * 1000 : n;
  return new Date(ms).toLocaleString();
}

export function epochOf(at: number | string | undefined): number {
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

// Exit-aware failure (issue #473): /api/jobs rows carry no
// status/state field, so status is always "done"/"pending" and a
// status regex alone never matches. A finished job with a nonzero
// exit is a failure; keep the regex for kinds without exits.
function jobFailed(j: JobRecord, status: string): boolean {
  if (j.done === true && typeof j.exit_code === 'number' && j.exit_code !== 0) return true;
  return /fail|error/i.test(status);
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
    failed: jobFailed(j, status),
    routine: false,
    detail: lines.join('\n'),
    runId: j.id,
  };
}

function entryFailed(e: LedgerEntry, raw: string, summary: string): boolean {
  if (e.ok === false) return true;
  if (typeof e.exit === 'number' && e.exit !== 0) return true;
  if (e.status !== undefined && /fail|error/i.test(String(e.status))) return true;
  return isFailure(e.kind, raw, summary);
}

function firstLine(text: string | undefined): string {
  return (text ?? '').split('\n')[0].trim();
}

// Short outcome clause for collapsed non-routine rows, from the
// informative fields expansion already shows. Agent-turn details
// lead with an "asked:" echo of the prompt; drop the prefix so the
// clause reads as the task, led by the provider.
function outcomeTask(e: LedgerEntry): string {
  const detail = firstLine(e.detail).replace(/^asked:\s+/i, '');
  return detail || firstLine(e.prompt) || firstLine(e.command);
}

function outcomeClause(e: LedgerEntry): string {
  const task = outcomeTask(e);
  const clean = task ? plainLanguage(task, e.kind) : '';
  if (e.kind !== 'agent-turn') return clean;
  const who = (e.provider ?? '').trim();
  if (who && clean) return `${who}: ${clean}`;
  return who || clean;
}

// Display badge for the kind slot: CLI turns read as their source,
// every other kind verbatim.
export function kindBadge(kind: string): string {
  return kind === 'cli-turn' ? 'cli' : kind;
}

// Dollars trimmed for phone lines: $5 not $5.0000, $0.0008 kept.
// (Same trim as ChatView's run line; the history row only needs the
// number, not the full per-row cost slot.)
function usd(n: number): string {
  return `$${n.toFixed(4).replace(/\.?0+$/, '')}`;
}

function cliCost(e: LedgerEntry): string | null {
  if (e.metered === false) return 'free';
  if (typeof e.cost_usd !== 'number') return null;
  return usd(e.cost_usd);
}

// Collapsed summary for a CLI turn, mirroring ChatView's run-line
// shape (script · model · duration · tokens · cost). Segments drop
// out individually when the row lacks them.
function cliTurnSummary(e: LedgerEntry): string {
  const parts = [e.script, e.model].filter((v): v is string => !!v);
  if (typeof e.duration_s === 'number') parts.push(`${e.duration_s}s`);
  const total = e.usage?.total_tokens;
  if (typeof total === 'number') parts.push(`${total} tok`);
  const cost = cliCost(e);
  if (cost) parts.push(cost);
  return parts.join(' · ') || 'cli turn';
}

function cliTurnLines(e: LedgerEntry): string[] {
  const lines = [`event: cli-turn ${e.script ?? 'turn'}`];
  if (e.thread_id) lines.push(`thread: ${e.thread_id}`);
  if (e.model) lines.push(`model: ${e.model}`);
  if (typeof e.duration_s === 'number') lines.push(`duration: ${e.duration_s}s`);
  const u = e.usage;
  if (u && typeof u.total_tokens === 'number') {
    lines.push(`tokens: ${u.total_tokens} (in ${u.input_tokens ?? 0}, out ${u.output_tokens ?? 0})`);
  }
  const cost = cliCost(e);
  if (cost) lines.push(`cost: ${cost}`);
  if (e.tools && e.tools.length > 0) lines.push(`tools: ${e.tools.join(', ')}`);
  return lines;
}

function ledgerToEntry(e: LedgerEntry): FlatEntry {
  if (e.kind === 'cli-turn') {
    return {
      kind: e.kind,
      summary: cliTurnSummary(e),
      at: rowTime(e.ts ?? e.at),
      rawAt: epochOf(e.ts ?? e.at),
      failed: entryFailed(e, 'cli-turn', cliTurnSummary(e)),
      routine: false,
      detail: cliTurnLines(e).join('\n').slice(0, 2000),
    };
  }
  const raw = e.event ?? e.action ?? e.kind;
  // Backend-supplied reasons are already human sentences; use verbatim.
  const errText = e.error?.trim() ?? '';
  const base = errText ? errText.slice(0, 200) : plainLanguage(raw ?? '', e.kind);
  const routine = ROUTINE_RE.test(base);
  let summary = base;
  if (!errText && !routine) {
    const clause = outcomeClause(e);
    const lower = clause.toLowerCase();
    const baseLower = base.toLowerCase();
    if (clause && lower !== baseLower && !lower.startsWith(baseLower)) {
      summary = `${base} — ${clause}`;
    }
  }
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
  // agent-turn rows record wall-clock latency in duration_s (the
  // cli-turn builder above renders its own); cost rides along when
  // present. Both drop out when absent — local turns show neither.
  if (typeof e.duration_s === 'number') lines.push(`duration: ${e.duration_s}s`);
  const cost = cliCost(e);
  if (cost) lines.push(`cost: ${cost}`);
  return {
    kind: e.kind,
    summary,
    at: rowTime(e.ts ?? e.at),
    rawAt: epochOf(e.ts ?? e.at),
    failed: entryFailed(e, raw ?? '', base),
    routine,
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

// One casing convention for collapsed summaries (issue #572):
// backend event text arrives lowercase ('chat started') while
// frontend labels are capitalized ('Chat session activity').
// Capitalize the first letter at display; the rest stays verbatim
// so model names, paths, and error detail are never mangled.
// Grouping still keys on the raw summary, so counts are unchanged.
export function sentenceCase(text: string): string {
  if (!text) return text;
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function jobPassesKindFilter(jobKind: string | undefined, filter: string): boolean {
  if (filter === 'all') return true;
  // Job rows are runs: they answer to the run filter whatever action ran.
  if (filter === 'run') return true;
  return (jobKind || 'job') === filter;
}

// Client-side text match over one collapsed row: summary, kind,
// and every member's detail block (the expansion text). Mirrors
// the Review row filter: case-insensitive substring, leading '#'
// ignored, empty query matches all.
export function historyRowMatches(row: HistoryRow, query: string): boolean {
  const q = query.trim().replace(/^#+/, '').toLowerCase();
  if (!q) return true;
  if (row.summary.toLowerCase().includes(q)) return true;
  if (row.kind.toLowerCase().includes(q)) return true;
  return row.members.some((m) => m.detail.toLowerCase().includes(q));
}

// Faceted instant filters (issue #839): All plus Errors (tone
// bad), Agent runs (turn rows), CI (runs, pulls, merges), and
// Cron (scheduled rows). Kind-based over collapsed rows; counts
// derive from the same filtered rows the feed shows.
export type HistoryFacet = 'all' | 'errors' | 'agents' | 'ci' | 'cron';

export interface HistoryFacetDef {
  id: HistoryFacet;
  label: string;
}

export const HISTORY_FACETS: HistoryFacetDef[] = [
  { id: 'all', label: 'All' },
  { id: 'errors', label: 'Errors' },
  { id: 'agents', label: 'Agent runs' },
  { id: 'ci', label: 'CI' },
  { id: 'cron', label: 'Cron' },
];

const AGENT_FACET_KINDS = new Set(['agent-turn', 'cli-turn']);
const CI_FACET_KINDS = new Set(['run', 'job', 'pr-action', 'webhook', 'repair']);

export function rowInFacet(row: HistoryRow, facet: HistoryFacet): boolean {
  switch (facet) {
    case 'all':
      return true;
    case 'errors':
      return row.tone === 'bad';
    case 'agents':
      return AGENT_FACET_KINDS.has(row.kind);
    case 'ci':
      return CI_FACET_KINDS.has(row.kind);
    case 'cron':
      return row.kind === 'cron' || row.kind === 'schedule';
  }
}

export function facetCounts(rows: readonly HistoryRow[]): Record<HistoryFacet, number> {
  const counts: Record<HistoryFacet, number> = {
    all: rows.length,
    errors: 0,
    agents: 0,
    ci: 0,
    cron: 0,
  };
  for (const row of rows) {
    if (row.tone === 'bad') counts.errors += 1;
    if (AGENT_FACET_KINDS.has(row.kind)) counts.agents += 1;
    if (CI_FACET_KINDS.has(row.kind)) counts.ci += 1;
    if (row.kind === 'cron' || row.kind === 'schedule') counts.cron += 1;
  }
  return counts;
}

// Facets worth a chip: All + Errors always (the error count
// reassures at zero); the rest only when rows exist for them.
export function visibleFacets(counts: Record<HistoryFacet, number>): HistoryFacetDef[] {
  return HISTORY_FACETS.filter(
    (f) => f.id === 'all' || f.id === 'errors' || counts[f.id] > 0,
  );
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
    summary: sentenceCase(routineRun ? activityLabel(entry.kind, count) : (repeated ?? entry.summary)),
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

// Shared failure predicate (issue #506): true when one ledger
// entry renders tone bad on its own row. Delegates to
// toHistoryRows so Briefing and History share one definition;
// per-entry (no incident-collapse) so the trend counts entries,
// not collapsed rows.
export function isHistoryFailure(entry: LedgerEntry): boolean {
  const rows = toHistoryRows([], [entry]);
  return rows.length > 0 && rows[0].tone === 'bad';
}
