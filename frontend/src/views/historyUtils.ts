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
  // Strip log-line noise: take at most the first short clause, no stack traces.
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
  recoveryHref: string;
}

function recoveryFor(kind: string, summary: string): { label: string; href: string } {
  const s = `${kind} ${summary}`.toLowerCase();
  if (s.includes('fast-forward') || s.includes('remote has new'))
    return { label: 'How to recover: pull, resolve, push', href: 'https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/addressing-merge-conflicts/resolving-a-merge-conflict-using-the-command-line' };
  if (s.includes('browser') || s.includes('playwright'))
    return { label: 'How to recover: reinstall browsers and rerun', href: 'https://playwright.dev/docs/browsers' };
  if (s.includes('network') || s.includes('could not be reached'))
    return { label: 'How to recover: retry when the service is reachable', href: '/api/health' };
  if (s.includes('not permitted') || s.includes('access'))
    return { label: 'How to recover: check access and retry', href: '/api/health' };
  if (s.includes('conflict'))
    return { label: 'How to recover: review and resolve conflicts', href: 'https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/addressing-merge-conflicts' };
  return { label: 'How to recover: check status and retry', href: '/api/health' };
}

function rowTime(at: number | string | undefined): string {
  if (at === undefined || at === null || at === '') return 'unknown time';
  const n = typeof at === 'string' ? Date.parse(at) : at;
  if (Number.isNaN(n)) return String(at);
  // Backend ledger uses epoch seconds; Date needs ms.
  const ms = n < 1e12 ? n * 1000 : n;
  return new Date(ms).toLocaleString();
}

/** Collapse consecutive identical (kind + summary) failures into incident rows. */
export function toHistoryRows(
  jobs: JobRecord[],
  entries: LedgerEntry[],
): HistoryRow[] {
  const flat: Array<{ kind: string; summary: string; at: string; rawAt: number }> = [];
  for (const j of jobs) {
    const status = (j as { status?: string }).status ?? (j.done ? 'done' : 'pending');
    const title = (j as { title?: string }).title ?? j.id;
    flat.push({
      kind: j.kind || 'job',
      summary: plainLanguage(`${title}: ${status}`, 'Job'),
      at: status,
      rawAt: 0,
    });
  }
  for (const e of entries) {
    const raw = e.event ?? e.kind;
    flat.push({
      kind: e.kind,
      summary: plainLanguage(raw ?? '', e.kind),
      at: rowTime(e.at),
      rawAt: typeof e.at === 'number' ? e.at : Date.parse(String(e.at ?? '')) || 0,
    });
  }
  // Ledger newest-first when timestamps exist; keep job rows first, then ledger order.
  const rows: HistoryRow[] = [];
  let i = 0;
  while (i < flat.length) {
    let count = 1;
    while (
      i + count < flat.length &&
      flat[i + count].kind === flat[i].kind &&
      flat[i + count].summary === flat[i].summary
    ) {
      count++;
    }
    const rec = recoveryFor(flat[i].kind, flat[i].summary);
    rows.push({
      key: `${flat[i].kind}-${flat[i].summary}-${i}`,
      kind: flat[i].kind,
      summary:
        count > 1
          ? `${flat[i].summary} ×${count} — same failure repeated`
          : flat[i].summary,
      at: flat[i].at,
      repeat: count,
      recovery: rec.label,
      recoveryHref: rec.href,
    });
    i += count;
  }
  return rows;
}
