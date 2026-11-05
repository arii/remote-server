import { useEffect, useRef, useState } from 'react';
import { fetchBriefing, fetchDigestSchedule, fetchLedger, fetchNotificationPreferences, fetchNotificationStatus, fetchPlan, fetchPlans, invalidateViewCaches, refreshBriefing, saveNotificationPreferences, setDigestSchedule, triggerDigest } from '../apiClient';
import type { DigestChannelResult, DigestSchedule, NotificationPreferences, NotificationsStatus, PlanListRow } from '../apiClient';
import type { BoardItem, BriefingResponse, LedgerEntry } from '../apiTypes';
import { isHistoryFailure, toHistoryRows } from './historyUtils';
import { loadBriefing, loadHistory, useMock } from './mockViews';
import RepoLabel from '../components/RepoLabel';

// Briefing lines come from the legacy briefing_summary passthrough and may
// carry reviewer HTML-comment markers, markdown list markers, full reviewer
// markdown (headings, quotes, bold, links), HTML-escaped quote entities, or
// links cut mid-URL upstream — or run long. Present the human part as one
// plain-text line, truncated at a word boundary (issue #574).
export function cleanBriefingLine(line: string): string {
  const stripped = line
    .replace(/<!--[\s\S]*?-->/g, ' ')
    .replace(/ai-review-state:\s*\S+/g, ' ')
    .replace(/<!--[\s\S]*$/g, ' ')
    // Escaped quote markers arrive mid-line once review newlines collapse
    // upstream: strip marker-position &gt;, decode glued stragglers.
    .replace(/(^|\s)&gt;(?=[\s#]|$)/gm, '$1')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#0?39;/g, "'")
    .replace(/&amp;/g, '&')
    .replace(/^\s{0,3}#{1,6}\s+/gm, '')
    .replace(/^\s{0,3}(?:>\s?)+/gm, '')
    .replace(/^\s{0,3}#{1,6}\s+/gm, '')
    .replace(/>+(?=\s*#{1,6}\s)/g, '')
    .replace(/^\s*([-*_])\1{2,}\s*$/gm, ' ')
    .replace(/^\s*(?:[-*+]|\d+[.)])\s+/gm, '')
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]*)\]\[[^\]]*\]/g, '$1')
    // Links cut mid-URL upstream never close; keep the link text.
    .replace(/!\[([^\]]*)\]\([^\s)]*$/g, '$1')
    .replace(/\[([^\]]*)\]\([^\s)]*$/g, '$1')
    .replace(/`([^`]*)`/g, '$1')
    .replace(/\*\*\*([^*]+)\*\*\*/g, '$1')
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/__([^_]+)__/g, '$1')
    .replace(/~~([^~]+)~~/g, '$1')
    .replace(/\*([^*\s][^*]*?)\*/g, '$1')
    .replace(/\b_([^_\s][^_]*?)_\b/g, '$1')
    .replace(/(^|\s)#{1,6}\s+/g, '$1')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/^[-*]\s+/, '');
  if (stripped.length <= 280) return stripped;
  const head = stripped.slice(0, 279);
  const cut = head.lastIndexOf(' ');
  const kept = (cut > 0 ? head.slice(0, cut) : head).replace(/\s+$/g, '');
  return `${kept}…`;
}

function Lines({ lines }: { lines: string[] }) {
  return (
    <ul className="session-list">
      {lines.map((line, i) => (
        // eslint-disable-next-line react/no-array-index-key
        <li key={i} className="detail">
          <p>{cleanBriefingLine(line)}</p>
        </li>
      ))}
    </ul>
  );
}

function Section({ title, lines, err }: { title: string; lines: string[]; err?: string }) {
  if (lines.length === 0 && !err) return null;
  return (
    <section>
      <h3>{title}</h3>
      {err && (
        <p className="row-meta" role="alert">
          {err}
        </p>
      )}
      {lines.length === 1 && !err ? (
        <p>{cleanBriefingLine(lines[0])}</p>
      ) : (
        <Lines lines={lines} />
      )}
    </section>
  );
}

// Board rows whose branch already has an open-PR pipeline row
// hide here: the pipeline owns branch display, the Board keeps
// only branches with no PR row (issue #839).
export function visibleBoardItems(board: BoardItem[], prBranches: readonly string[]): BoardItem[] {
  if (prBranches.length === 0) return board;
  const have = new Set(prBranches.map((b) => b.trim()).filter(Boolean));
  return board.filter((item) => !have.has((item.branch ?? '').trim()));
}

export function boardStatusForBranch(branch: string, board: BoardItem[]): string {
  const want = branch.trim();
  if (!want) return '';
  const hit = board.find((item) => (item.branch ?? '').trim() === want);
  const status = (hit?.status ?? '').trim();
  return status === '?' ? '' : status;
}

// Issue-chip hop (issue #963): a board `issue N` chip jumps to
// the in-app Review section, where the issues list lives.
// Branches stay static text — no branch view exists, and a
// button to nowhere would be a dead control.
export function boardIssueNumber(chip: string): string {
  const m = chip.match(/issue\s+#?(\d+)/i);
  return m ? m[1] : '';
}

function BoardSection({
  board,
  lines,
  err,
  onOpenReview,
}: {
  board: BoardItem[];
  lines: string[];
  err?: string;
  onOpenReview?: (prNumber: string) => void;
}) {
  if (board.length === 0) {
    return <Section title="Board" lines={lines} err={err} />;
  }
  return (
    <section>
      <h3>Board</h3>
      {err && (
        <p className="row-meta" role="alert">
          {err}
        </p>
      )}
      <ul className="session-list" data-testid="board-chips">
        {board.map((b, i) => {
          const chips = [b.branch, b.issue ? `issue ${b.issue}` : '', b.status]
            .map((v) => (v ?? '').trim())
            .filter((v) => v.length > 0 && v !== '?');
          // eslint-disable-next-line react/no-array-index-key
          return (
            <li key={i} className="detail">
              <div className="chip-row">
                {chips.map((c) => {
                  const issueNum = boardIssueNumber(c);
                  if (issueNum && onOpenReview) {
                    return (
                      <button
                        key={c}
                        type="button"
                        className="repo-chip link-chip"
                        data-testid="briefing-board-issue"
                        onClick={() => onOpenReview(issueNum)}
                        title={`Open issue #${issueNum} in Review`}
                      >
                        {truncateChip(c)}
                      </button>
                    );
                  }
                  return (
                    <span key={c} className="repo-chip" title={c.length > 32 ? c : undefined}>
                      {truncateChip(c)}
                    </span>
                  );
                })}
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

// Board chip text: branch names run to 50+ chars and wrap the
// chip row, so over-long chips truncate with the full text kept
// in the title. Short chips render verbatim (no title needed).
export function truncateChip(text: string, max: number = 32): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

// One-line condensation of briefing state, always visible.
export function briefingSummary(data: BriefingResponse): string {
  if (briefingAllClear(data)) return 'All clear — nothing open.';
  const parts: string[] = [];
  if (data.prs.length > 0) {
    parts.push(`${data.prs.length} open PR${data.prs.length === 1 ? '' : 's'}`);
  }
  if (data.attention.length > 0) {
    parts.push(`${data.attention.length} flagged`);
  }
  if (data.conflicts_lines.length > 0) {
    parts.push(
      `${data.conflicts_lines.length} conflict${data.conflicts_lines.length === 1 ? '' : 's'}`,
    );
  }
  if (data.workflow_lines.length > 0) {
    parts.push(
      `${data.workflow_lines.length} failing run${data.workflow_lines.length === 1 ? '' : 's'}`,
    );
  }
  return parts.join(' · ');
}

// Freshness line for cached payloads: the endpoint serves repeat
// visits from its stale-while-revalidate cache, so say how old the
// data is (and that a refresh is on its way when stale).
export function briefingFreshness(data: BriefingResponse, nowMs: number = Date.now()): string | null {
  if (!data.cached && !data.stale) return null;
  const at = typeof data.generated_at === 'number' ? data.generated_at : nowMs / 1000;
  const ageS = Math.max(0, Math.round(nowMs / 1000 - at));
  const age = ageS < 60 ? `${ageS}s` : `${Math.floor(ageS / 60)}m`;
  return data.stale ? `Updated ${age} ago · refreshing…` : `Updated ${age} ago`;
}

export interface BriefingAction {
  label: string;
  seed: string;
}

// Manual digest controls (issue #615). The trigger endpoint answers
// ok:true even with nowhere to notify, so every user-facing line
// derives from the configured channels and per-channel results —
// never from the ok flag (regression guard vs #430) and never from
// the last_digest stamp alone (vs #532).
export function digestChannels(status: NotificationsStatus): string[] {
  const channels: string[] = [];
  if (status.ntfy_configured) channels.push('ntfy');
  if (status.webhook_configured) channels.push('webhook');
  return channels;
}

export function formatDigestStamp(epochSeconds: number): string {
  if (!epochSeconds) return 'never sent';
  return new Date(epochSeconds * 1000).toLocaleString();
}

export function digestUnconfiguredReason(): string {
  return (
    'Notifications not configured — set NTFY_TOPIC or NOTIFICATION_WEBHOOK_URL ' +
    '(see docs/buttons.md).'
  );
}

export function digestDisabledReason(
  status: NotificationsStatus | null,
  statusErr: string | null,
  running: boolean,
): string | null {
  if (running) return 'Sending digest…';
  if (statusErr) return 'Notification status unavailable — cannot confirm a channel is configured.';
  if (!status) return 'Loading notification status…';
  if (digestChannels(status).length === 0) return digestUnconfiguredReason();
  return null;
}

export function digestStatusLine(status: NotificationsStatus): string {
  const channels = digestChannels(status);
  if (channels.length === 0) return digestUnconfiguredReason();
  if (!status.last_digest) return `No digest sent yet · channels: ${channels.join(', ')}`;
  return `Last digest ${formatDigestStamp(status.last_digest)} · via ${channels.join(', ')}`;
}

export function digestResultLine(
  results: Record<string, DigestChannelResult>,
  sentAt: string,
): string {
  const raw = (results ?? {}) as unknown as Record<string, unknown>;
  if (typeof raw.suppressed === 'string') {
    return `Digest not sent — ${raw.suppressed}.`;
  }
  const entries = Object.entries(results ?? {});
  if (entries.length === 0) {
    return 'Digest built but delivered nowhere — no channel reported a send.';
  }
  const okNames = entries.filter(([, r]) => r.ok).map(([name]) => name);
  const bad = entries
    .filter(([, r]) => !r.ok)
    .map(([name, r]) => `${name} failed (${r.error || 'unknown error'})`);
  if (bad.length === 0) return `Digest sent ${sentAt} via ${okNames.join(', ')}.`;
  if (okNames.length === 0) return `Digest failed — ${bad.join('; ')}.`;
  return `Digest partially sent ${sentAt} — ${okNames.join(', ')} ok; ${bad.join('; ')}.`;
}

const DIGEST_CADENCES = [
  { value: 'daily', label: 'Daily' },
  { value: 'weekly', label: 'Weekly' },
  { value: 'off', label: 'Off' },
] as const;

export function digestScheduleLine(sched: DigestSchedule): string {
  if (sched.cadence !== 'daily' && sched.cadence !== 'weekly') return 'Scheduled digests off.';
  const next = sched.next_run
    ? `Next digest ${formatDigestStamp(sched.next_run)}`
    : 'Next digest not scheduled';
  const res = sched.last_result ?? {};
  if (!res.at) return `${next}.`;
  if (res.ok) return `${next} · last sent ${formatDigestStamp(res.at)}.`;
  const err = res.error || 'unknown error';
  const short = err.length > 120 ? `${err.slice(0, 120)}…` : err;
  return `${next} · last scheduled send failed (${short}).`;
}

export function DigestControlsView({
  status,
  statusErr,
  running,
  result,
  onRun,
  sched = null,
  schedErr = null,
  schedBusy = false,
  onSetCadence = () => {},
}: {
  status: NotificationsStatus | null;
  statusErr: string | null;
  running: boolean;
  result: string | null;
  onRun: () => void;
  sched?: DigestSchedule | null;
  schedErr?: string | null;
  schedBusy?: boolean;
  onSetCadence?: (cadence: string) => void;
}) {
  const reason = digestDisabledReason(status, statusErr, running);
  const line = statusErr ? `Digest status unavailable — ${statusErr}` : !status ? 'Digest: checking status…' : digestStatusLine(status);
  const schedLine = schedErr
    ? `Digest schedule unavailable — ${schedErr}`
    : !sched
      ? 'Digest schedule: checking…'
      : digestScheduleLine(sched);
  return (
    <section aria-label="Briefing digest">
      <div className="actions">
        <button
          type="button"
          data-testid="briefing-run-now"
          disabled={reason !== null}
          title={reason ?? undefined}
          onClick={onRun}
        >
          {running ? 'Sending…' : 'Run briefing now'}
        </button>{' '}
        <div className="actions" role="group" aria-label="Digest schedule" data-testid="briefing-digest-schedule">
          {DIGEST_CADENCES.map((c) => (
            <button
              key={c.value}
              type="button"
              data-testid={`briefing-schedule-${c.value}`}
              aria-pressed={sched?.cadence === c.value}
              disabled={schedBusy}
              onClick={() => onSetCadence(c.value)}
            >
              {c.label}
            </button>
          ))}
        </div>
      </div>
      <p className="row-meta" data-testid="briefing-digest-status">
        {line}
      </p>
      <p className="row-meta" data-testid="briefing-digest-schedule-status">
        {schedLine}
      </p>
      {result && (
        <p className="row-meta" data-testid="briefing-digest-result">
          {result}
        </p>
      )}
    </section>
  );
}

export function DigestControls() {
  const [status, setStatus] = useState<NotificationsStatus | null>(null);
  const [statusErr, setStatusErr] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [sched, setSched] = useState<DigestSchedule | null>(null);
  const [schedErr, setSchedErr] = useState<string | null>(null);
  const [schedBusy, setSchedBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const live = await fetchNotificationStatus();
        if (!cancelled) setStatus(live);
      } catch (err) {
        if (!cancelled) setStatusErr(err instanceof Error ? err.message : String(err));
      }
      try {
        const liveSched = await fetchDigestSchedule();
        if (!cancelled) setSched(liveSched);
      } catch (err) {
        if (!cancelled) setSchedErr(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function runNow() {
    if (running) return;
    setRunning(true);
    setResult(null);
    try {
      const res = await triggerDigest();
      setResult(digestResultLine(res.results, new Date().toLocaleString()));
    } catch (err) {
      setResult(`Digest failed — ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      try {
        setStatus(await fetchNotificationStatus());
        setStatusErr(null);
      } catch {
        // Keep the last known status; the result line already reports
        // this run's outcome.
      }
      setRunning(false);
    }
  }

  async function setCadence(cadence: string) {
    if (schedBusy) return;
    setSchedBusy(true);
    try {
      setSched(await setDigestSchedule(cadence));
      setSchedErr(null);
    } catch (err) {
      setSchedErr(err instanceof Error ? err.message : String(err));
    } finally {
      setSchedBusy(false);
    }
  }

  return (
    <DigestControlsView
      status={status}
      statusErr={statusErr}
      running={running}
      result={result}
      onRun={() => void runNow()}
      sched={sched}
      schedErr={schedErr}
      schedBusy={schedBusy}
      onSetCadence={(c) => void setCadence(c)}
    />
  );
}

// Notification preferences (issue #615's digest cluster, issue
// #656). One prefs object, one round trip.
export function prefsStatusLine(prefs: NotificationPreferences): string {
  const muted = prefs.muted_events.length > 0
    ? `Muted: ${prefs.muted_events.join(', ')}`
    : 'All events on';
  return `${muted}.`;
}

export function PrefsControlsView({
  prefs,
  prefsErr,
  busy,
  onToggle,
}: {
  prefs: NotificationPreferences | null;
  prefsErr: string | null;
  busy: boolean;
  onToggle: (event: string) => void;
}) {
  const line = prefsErr
    ? `Notification preferences unavailable — ${prefsErr}`
    : !prefs
      ? 'Notification preferences: checking…'
      : prefsStatusLine(prefs);
  return (
    <section aria-label="Notification preferences">
      <div
        className="actions"
        role="group"
        aria-label="Muted events"
        data-testid="briefing-prefs-events"
      >
        {(prefs?.events ?? []).map((e) => (
          <button
            key={e}
            type="button"
            data-testid={`briefing-prefs-mute-${e}`}
            aria-pressed={(prefs?.muted_events ?? []).includes(e)}
            disabled={busy || !prefs}
            title={e === 'digest' ? 'Mute digest pushes (scheduled + manual)' : `Mute ${e} pushes`}
            onClick={() => onToggle(e)}
          >
            {e}
          </button>
        ))}
      </div>
      <p className="row-meta" data-testid="briefing-prefs-status">
        {line}
      </p>
    </section>
  );
}

export function PrefsControls() {
  const [prefs, setPrefs] = useState<NotificationPreferences | null>(null);
  const [prefsErr, setPrefsErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const live = await fetchNotificationPreferences();
        if (!cancelled) setPrefs(live);
      } catch (err) {
        if (!cancelled) setPrefsErr(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function save(body: { muted_events?: string[] }) {
    if (busy) return;
    setBusy(true);
    try {
      setPrefs(await saveNotificationPreferences(body));
      setPrefsErr(null);
    } catch (err) {
      setPrefsErr(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  function toggle(event: string) {
    if (!prefs) return;
    const muted = prefs.muted_events.includes(event)
      ? prefs.muted_events.filter((e) => e !== event)
      : [...prefs.muted_events, event];
    void save({ muted_events: muted });
  }

  return (
    <PrefsControlsView
      prefs={prefs}
      prefsErr={prefsErr}
      busy={busy}
      onToggle={(e) => toggle(e)}
    />
  );
}

// Next actions derived from state, most urgent first, capped at three.
// Each stages its seed in agent chat's composer for review —
// tapping a seed never auto-sends (#507).
export function briefingActions(data: BriefingResponse): BriefingAction[] {
  const actions: BriefingAction[] = [];
  if (data.workflow_lines.length > 0) {
    actions.push({
      label: 'Explain the failures',
      seed: 'What failed recently and how do I fix it?',
    });
  }
  if (data.conflicts_lines.length > 0) {
    actions.push({
      label: 'Resolve the conflicts',
      seed: 'How do I resolve these merge conflicts?',
    });
  }
  if (data.prs.length > 0) {
    actions.push({
      label: 'Summarize open PRs',
      seed: 'Summarize the open pull requests.',
    });
  }
  if (data.attention.length > 0) {
    actions.push({
      label: 'Triage attention items',
      seed: 'Help me triage these attention items.',
    });
  }
  if (actions.length === 0) {
    actions.push({
      label: 'Plan my day',
      seed: 'What should I work on next?',
    });
  }
  return actions.slice(0, 3);
}

// Activity window presets (issue #609): the Recent activity
// section counts + rows narrow to one fetch with ?since=.
export type ActivityWindow = '1h' | '24h' | '7d';

export const ACTIVITY_WINDOWS: Array<{ value: ActivityWindow; label: string; seconds: number }> = [
  { value: '1h', label: '1h', seconds: 3600 },
  { value: '24h', label: '24h', seconds: 86400 },
  { value: '7d', label: '7d', seconds: 604800 },
];

export function activityWindowSeconds(window: ActivityWindow): number {
  return ACTIVITY_WINDOWS.find((o) => o.value === window)?.seconds ?? 86400;
}

// Client-side window filter: the server pre-filters live reads via
// ?since=, but mock data arrives unfiltered, so rows + counts narrow
// here in every mode.
export function inActivityWindow(
  entries: LedgerEntry[],
  window: ActivityWindow,
  nowSec: number,
): LedgerEntry[] {
  const cutoff = nowSec - activityWindowSeconds(window);
  return entries.filter((e) => typeof e.ts === 'number' && e.ts >= cutoff);
}

// Activity counts from ledger timestamps, labelled by window.
// Failures reuse the History tone predicate (issue #506): only
// webhook entries carry ok, so ok-only missed run/chat/agent
// failures and the two views disagreed on the same ledger.
export function dayTrend(
  entries: LedgerEntry[],
  nowSec: number,
  window: ActivityWindow = '24h',
): string {
  const recent = inActivityWindow(entries, window, nowSec);
  const failures = recent.filter(isHistoryFailure).length;
  return `Last ${window}: ${recent.length} event${recent.length === 1 ? '' : 's'}, ${failures} failure${failures === 1 ? '' : 's'}.`;
}

// Reviews errors arrive as a per-PR dict ({PR number: error});
// condense to one alert line for the Reviews section (issue #463).
export function reviewsErrText(err: Record<string, string> | null | undefined): string {
  if (!err) return '';
  const entries = Object.entries(err);
  if (entries.length === 0) return '';
  return entries
    .sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true }))
    .map(([num, msg]) => `#${num}: ${msg}`)
    .join('; ');
}

// Condense-empties: when every section is empty and nothing errored,
// the view shows one all-clear instead of five bare headings.
export function briefingAllClear(data: BriefingResponse): boolean {
  if (data.prs_err || data.board_err || data.runs_err || data.conf_err) return false;
  if (data.reviews_err && Object.keys(data.reviews_err).length > 0) return false;
  return (
    data.prs.length === 0 &&
    data.attention.length === 0 &&
    data.reviews_lines.length === 0 &&
    (data.board ?? []).length === 0 &&
    data.board_lines.length === 0 &&
    data.conflicts_lines.length === 0 &&
    data.overlaps_lines.length === 0 &&
    data.workflow_lines.length === 0
  );
}

// First review line referencing one PR number, cleaned for
// display. Reviews arrive as free text keyed by PR in prose.
export function prReviewNote(prNumber: string, reviewsLines: readonly string[]): string {
  const want = `#${prNumber}`;
  for (const line of reviewsLines) {
    if (line.includes(want)) return cleanBriefingLine(line);
  }
  return '';
}

// Bot verdict badge (issue #839): review notes bury a
// "Verdict: X" line in prose/markdown. An all-caps verdict
// phrase lifts into a badge; anything else stays note-only
// rather than risk a false badge.
export function prReviewVerdict(note: string): string {
  const m = note.match(/[Vv]erdict:\s*([A-Z]{2,}(?:[ ]+[A-Z]{2,})*)/);
  if (!m) return '';
  return m[1];
}

// Identical failure lines collapse into one counted row, in first-
// seen order (issue #839). Exact-text match only — near-identical
// lines stay separate rather than risk a false merge.
export interface BriefingLineGroup {
  text: string;
  count: number;
}

export function groupBriefingLines(lines: readonly string[]): BriefingLineGroup[] {
  const groups: BriefingLineGroup[] = [];
  const index = new Map<string, number>();
  for (const line of lines) {
    const at = index.get(line);
    if (at === undefined) {
      index.set(line, groups.length);
      groups.push({ text: line, count: 1 });
    } else {
      groups[at].count += 1;
    }
  }
  return groups;
}

export function GroupedLines({
  lines,
  onInvestigate,
}: {
  lines: string[];
  onInvestigate?: (group: BriefingLineGroup) => void;
}) {
  return (
    <ul className="session-list">
      {groupBriefingLines(lines).map((g, i) => (
        // eslint-disable-next-line react/no-array-index-key
        <li key={i} className="detail">
          <p>
            {cleanBriefingLine(g.text)}
            {g.count > 1 ? ` ×${g.count}` : ''}
          </p>
          {g.count > 1 && (
            <p className="row-meta">Same failure repeated {g.count} times.</p>
          )}
          {onInvestigate && (
            <div className="actions">
              <button
                type="button"
                className="btn-secondary"
                data-testid={`briefing-investigate-${i}`}
                onClick={() => onInvestigate(g)}
              >
                Investigate
              </button>
            </div>
          )}
        </li>
      ))}
    </ul>
  );
}

// Plans (issue #963): the .agents/plans index with per-plan
// step progress and one-tap Execute. Content loads lazily per
// plan, so a long index never fetches every file up front.
export function PlanStepsView({ steps, limit = 20 }: { steps: PlanStep[]; limit?: number }) {
  const shown = steps.slice(0, limit);
  return (
    <>
      <p className="row-meta" data-testid="briefing-plan-progress">
        {planProgressLine(steps)}
      </p>
      {steps.length > 0 && (
        <ul className="session-list">
          {shown.map((s, i) => (
            // eslint-disable-next-line react/no-array-index-key
            <li key={i} className="detail">
              <span className="row-title">
                [{s.checked ? 'x' : ' '}] {s.text || '(empty step)'}
              </span>
            </li>
          ))}
        </ul>
      )}
      {steps.length > limit && <p className="row-meta">+{steps.length - limit} more steps.</p>}
    </>
  );
}

function PlanRow({
  repo,
  plan,
  onAskAgent,
}: {
  repo: string;
  plan: PlanListRow;
  onAskAgent: (seed: string) => void;
}) {
  const [content, setContent] = useState<string | null>(null);
  const [contentErr, setContentErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function ensureContent() {
    if (content !== null || loading || useMock()) return;
    setLoading(true);
    try {
      const p = await fetchPlan(repo, plan.name);
      setContent(p.content);
      setContentErr(null);
    } catch (err) {
      setContentErr(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  const steps = content === null ? null : planSteps(content);
  return (
    <li className="detail" data-testid="briefing-plan-row">
      <span className="row-title">{plan.name}</span>
      <p className="row-meta pipeline-meta">{plan.size} bytes</p>
      <div className="actions">
        <button
          type="button"
          data-testid="briefing-plan-execute"
          onClick={() =>
            onAskAgent(
              `Execute the plan in ${plan.name} for ${repo}: read .agents/plans/${plan.name} and carry out each unchecked step, verifying with the repo's tests.`,
            )
          }
        >
          Execute
        </button>
      </div>
      <details
        onToggle={(e) => {
          if ((e.target as HTMLDetailsElement).open) void ensureContent();
        }}
      >
        <summary className="row-meta" data-testid="briefing-plan-steps-toggle">
          Steps{steps ? ` · ${planProgressLine(steps)}` : ''}
        </summary>
        {loading && (
          <p className="row-meta" role="status">
            Loading plan…
          </p>
        )}
        {contentErr && (
          <p className="row-meta" role="alert">
            Couldn&apos;t load plan: {contentErr}
          </p>
        )}
        {steps && <PlanStepsView steps={steps} />}
      </details>
    </li>
  );
}

export function PlansSection({
  repo,
  onAskAgent,
}: {
  repo: string;
  onAskAgent: (seed: string) => void;
}) {
  const [plans, setPlans] = useState<PlanListRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    if (useMock()) {
      setPlans([]);
      return;
    }
    (async () => {
      try {
        const live = await fetchPlans(repo);
        if (!cancelled) {
          setPlans(live.plans);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setPlans([]);
          setError(err instanceof Error ? err.message : String(err));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repo]);

  return (
    <section aria-label="Plans">
      <h3>Plans</h3>
      {error && (
        <p className="row-meta" role="alert" data-testid="briefing-plans-error">
          Couldn&apos;t load plans: {error}
        </p>
      )}
      {plans === null ? (
        <p className="row-meta" role="status">
          Checking plans…
        </p>
      ) : plans.length === 0 && !error ? (
        <p className="row-meta" data-testid="briefing-plans-empty">
          No plans in .agents/plans.
        </p>
      ) : (
        <ul className="session-list" data-testid="briefing-plans">
          {plans.map((p) => (
            <PlanRow key={p.name} repo={repo} plan={p} onAskAgent={onAskAgent} />
          ))}
        </ul>
      )}
    </section>
  );
}

// Repair picker (issue #963): open PRs listed with titles for
// the repair seed — a picker, never a blind number input.
export function RepairPicker({
  repo,
  prs,
  onAskAgent,
}: {
  repo: string;
  prs: Array<Record<string, unknown>>;
  onAskAgent: (seed: string) => void;
}) {
  const options = prs
    .map((pr) => ({
      number: String(pr.number ?? '').trim(),
      title: String(pr.title ?? 'Untitled'),
    }))
    .filter((o) => /^\d+$/.test(o.number));
  const [picked, setPicked] = useState(options.length === 1 ? options[0].number : '');

  return (
    <section aria-label="Repair a pull request">
      <h3>Repair</h3>
      {options.length === 0 ? (
        <p className="row-meta" data-testid="briefing-repair-empty">
          No open PRs to repair.
        </p>
      ) : (
        <div className="actions">
          <label className="row-meta" htmlFor="briefing-repair-pick">
            Pull request{' '}
            <select
              id="briefing-repair-pick"
              data-testid="briefing-repair-picker"
              value={picked}
              onChange={(e) => setPicked(e.target.value)}
            >
              <option value="">Select…</option>
              {options.map((o) => (
                <option key={o.number} value={o.number}>
                  #{o.number} {o.title}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            data-testid="briefing-repair-run"
            disabled={!picked}
            onClick={() => {
              const o = options.find((x) => x.number === picked);
              if (o) onAskAgent(prRepairSeed(repo, o.number, o.title));
            }}
          >
            Repair
          </button>
        </div>
      )}
    </section>
  );
}

// Checks tab for a PR web URL: trailing slashes trimmed so the
// suffix never doubles. Empty in, empty out (older payloads carry
// no URL; those pills fall back to the in-app Review section).
export function prChecksUrl(prUrl: string): string {
  const base = prUrl.trim().replace(/\/+$/, '');
  return base ? `${base}/checks` : '';
}

// One pipeline pill (#902): a link out when the backend shipped the
// PR's web URL, an in-app Review hop otherwise. Every pill navigates
// somewhere real — no dead pills.
function PrPill({
  testid,
  label,
  title,
  href,
  onOpenReview,
}: {
  testid: string;
  label: string;
  title: string;
  href: string;
  onOpenReview: () => void;
}) {
  if (href) {
    return (
      <a
        className="repo-chip link-chip"
        data-testid={testid}
        href={href}
        target="_blank"
        rel="noreferrer"
        title={title}
      >
        {label}
      </a>
    );
  }
  return (
    <button
      type="button"
      className="repo-chip link-chip"
      data-testid={testid}
      onClick={onOpenReview}
      title={title}
    >
      {label}
    </button>
  );
}

// Row-level agent seeds (issue #963): the same shape as the
// Review-card seeds (#962) so a briefing row hands the agent
// the same context the Review view would.
export function prAskSeed(repo: string, number: string, title: string): string {
  return `PR #${number} ${title} (${repo}): review the diff and implement the needed changes.`;
}

export function prRepairSeed(repo: string, number: string, title: string): string {
  return `In ${repo}, repair PR #${number} (${title}): investigate the cause, implement the fix, and verify with the repo's tests.`;
}

// Executable-plan steps (issue #963): checkbox lines are steps,
// `[x]`/`[X]` done. Anything else is prose, never a step.
export interface PlanStep {
  text: string;
  checked: boolean;
}

export function planSteps(content: string): PlanStep[] {
  const steps: PlanStep[] = [];
  for (const line of content.split('\n')) {
    const m = line.match(/^\s*[-*]\s+\[([ xX])\]\s*(.*)$/);
    if (m) steps.push({ text: m[2].trim(), checked: m[1].toLowerCase() === 'x' });
  }
  return steps;
}

export function planProgressLine(steps: readonly PlanStep[]): string {
  if (steps.length === 0) return 'No checkable steps.';
  const done = steps.filter((s) => s.checked).length;
  return `${done}/${steps.length} steps done.`;
}

function PrRow({
  pr,
  repo,
  board,
  reviewsLines,
  onOpenReview,
  onAskAgent,
}: {
  pr: Record<string, unknown>;
  repo: string;
  board: BoardItem[];
  reviewsLines: string[];
  onOpenReview: (prNumber: string) => void;
  onAskAgent: (seed: string) => void;
}) {
  const number = String(pr.number ?? '?');
  const title = String(pr.title ?? 'Untitled');
  const branch = String(pr.branch ?? '').trim();
  // Briefing PRs carry ci/merge/draft/stale (the Review list shape
  // uses ciLabel/ciStatus instead); read both, briefing first.
  const ci = String(pr.ci ?? pr.ciLabel ?? pr.ciStatus ?? '').trim();
  const merge = String(pr.merge ?? '').trim();
  const meta = [pr.author, branch]
    .map((v) => String(v ?? '').trim())
    .filter((v) => v.length > 0);
  if (merge) meta.push(`merge ${merge}`);
  if (pr.draft === true) meta.push('draft');
  if (pr.stale === true) meta.push('stale');
  const boardStatus = boardStatusForBranch(branch, board);
  if (boardStatus) meta.push(boardStatus);
  const ownReview = String(pr.review ?? '').trim();
  const note = /^\d+$/.test(number)
    ? prReviewNote(number, reviewsLines) || (ownReview ? cleanBriefingLine(ownReview) : '')
    : '';
  const verdict = note ? prReviewVerdict(note) : '';
  const prUrl = typeof pr.url === 'string' ? pr.url.trim() : '';
  const openReview = () => onOpenReview(number);
  return (
    <li className="detail" data-testid="briefing-pr-row">
      <button
        type="button"
        className="row-title link-chip"
        data-testid="briefing-pr-open"
        onClick={openReview}
        title={`Open #${number} in Review`}
      >
        #{number} {title}
      </button>
      {meta.length > 0 && <p className="row-meta pipeline-meta">{meta.join(' · ')}</p>}
      {(ci || verdict) && (
        <p className="row-meta chip-row">
          {ci && (
            <PrPill
              testid="briefing-ci"
              label={`CI ${ci}`}
              title={prUrl ? `Open checks for #${number}` : `Open #${number} in Review`}
              href={prChecksUrl(prUrl)}
              onOpenReview={openReview}
            />
          )}
          {verdict && (
            <PrPill
              testid="briefing-verdict"
              label={`Verdict: ${verdict}`}
              title={prUrl ? `Open PR #${number}` : `Open #${number} in Review`}
              href={prUrl}
              onOpenReview={openReview}
            />
          )}
        </p>
      )}
      {note && <p className="row-meta">Review: {note}</p>}
      <div className="actions">
        <button type="button" data-testid="briefing-ask-agent" onClick={() => onAskAgent(prAskSeed(repo, number, title))}>
          Ask agent
        </button>
      </div>
    </li>
  );
}

// Empty-state generate flow (#929): poll the refreshed snapshot
// until a newer payload lands, bounded so the loop always ends.
const GEN_POLL_MS = 5000;
const GEN_MAX_TRIES = 36;
// Loading stages (#929): past this the bare spinner names the
// wait, so a slow first generation never reads as stuck.
const SLOW_LOAD_MS = 10000;

export default function BriefingView({
  repo,
  onAskAgent,
  onOpenHistory,
  onOpenReview,
  reloadRequest = 0,
  active = true,
}: {
  repo: string;
  onAskAgent: (seed: string) => void;
  onOpenHistory: () => void;
  // Pipeline-pill fallback (#902): pills without a backend URL hop
  // to the in-app Review section instead of rendering dead.
  onOpenReview: (prNumber: string) => void;
  // Palette request (issue #810): a nonce bump re-fetches the
  // briefing while mounted. Mount already loads, so the opening
  // nonce is adopted, not acted on — only later bumps reload.
  reloadRequest?: number;
  // Keep-alive visibility (issue #897): refresh loops pause while
  // the tab is hidden; returning revalidates once.
  active?: boolean;
}) {
  const [data, setData] = useState<BriefingResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ledger, setLedger] = useState<LedgerEntry[] | null>(null);
  const [window, setWindow] = useState<ActivityWindow>('24h');
  const [staleTries, setStaleTries] = useState(0);
  // Generate flow (#929): genBase is the payload stamp at tap
  // time (null when idle); the loop ends on any newer stamp.
  const [genBase, setGenBase] = useState<number | null>(null);
  const [genTries, setGenTries] = useState(0);
  const [genError, setGenError] = useState<string | null>(null);
  const [slowLoad, setSlowLoad] = useState(false);
  useEffect(() => {
    setStaleTries(0);
    setGenBase(null);
    setGenTries(0);
    setGenError(null);
  }, [repo]);

  async function load() {
    setError(null);
    try {
      setData(await fetchBriefing(repo));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function generate() {
    if (genBase !== null) return;
    setGenError(null);
    try {
      await refreshBriefing(repo);
    } catch (err) {
      setGenError(err instanceof Error ? err.message : String(err));
      return;
    }
    invalidateViewCaches();
    setGenBase(data?.generated_at ?? 0);
    setGenTries(0);
    void load();
  }

  // Palette reload: adopt the mount-time nonce, act on later bumps.
  const handledReloadNonce = useRef(reloadRequest);
  useEffect(() => {
    if (reloadRequest !== handledReloadNonce.current) {
      handledReloadNonce.current = reloadRequest;
      void load();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reloadRequest]);

  // SWR-aware refresh (issue #897): a stale payload means the
  // server is regenerating in the background — re-fetch until
  // fresh, bounded, so the "refreshing…" indicator always resolves
  // instead of sitting on old data.
  useEffect(() => {
    if (!data || !data.stale || !active || useMock() || staleTries >= 3) return;
    // Bare timer globals: the activity `window` state shadows
    // window.* in this component.
    const t = setTimeout(() => {
      setStaleTries((n) => n + 1);
      void load();
    }, 5000);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, active, staleTries]);

  // Return revalidation (issue #897): with keep-alive the view
  // stays mounted while hidden — one cheap re-fetch on return
  // (fresh cache serves instantly; a stale flag starts the loop
  // above instead of showing old data silently).
  const wasActive = useRef(active);
  useEffect(() => {
    const returning = active && !wasActive.current;
    wasActive.current = active;
    if (returning && data && !useMock()) void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);

  // Generate poll (#929): bounded re-fetch until a post-tap
  // payload lands. Comparing server stamps keeps the loop
  // clock-skew-proof; content arriving early ends it too, and a
  // fresh-but-still-empty payload ends it as well (the empty
  // state re-renders with its action, never a spinner).
  useEffect(() => {
    if (genBase === null || useMock()) return;
    const stamp = data?.generated_at ?? 0;
    if (stamp > genBase || (data !== null && !briefingAllClear(data))) {
      setGenBase(null);
      return;
    }
    if (genTries >= GEN_MAX_TRIES) {
      setGenBase(null);
      setGenError('Generation is taking longer than expected — retry in a minute.');
      return;
    }
    // Bare timer global: the activity `window` state shadows
    // window.* in this component.
    const t = setTimeout(() => {
      setGenTries((n) => n + 1);
      invalidateViewCaches();
      void load();
    }, GEN_POLL_MS);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [genBase, genTries, data]);

  // Staged loading (#929): past SLOW_LOAD_MS the bare spinner
  // names the wait, so a slow first generation never reads as
  // stuck. The fetch timeout still bounds the total wait.
  useEffect(() => {
    if (data || error) {
      setSlowLoad(false);
      return;
    }
    const t = setTimeout(() => setSlowLoad(true), SLOW_LOAD_MS);
    return () => clearTimeout(t);
  }, [data, error, repo]);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    setError(null);
    setLedger(null);
    if (useMock()) {
      setData(loadBriefing());
      setLedger(loadHistory().entries);
      return;
    }
    (async () => {
      try {
        const live = await fetchBriefing(repo);
        if (!cancelled) setData(live);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repo]);

  // Secondary: recent-activity highlights, narrowed server-side to
  // the selected window (limit 200, the server max, so 7d keeps its
  // older rows). Failure degrades to the empty state instead of
  // breaking the briefing. Mock mode keeps its canned entries.
  useEffect(() => {
    let cancelled = false;
    if (useMock()) return;
    (async () => {
      try {
        const since = Date.now() / 1000 - activityWindowSeconds(window);
        const live = await fetchLedger('all', 200, since);
        if (!cancelled) setLedger(live.entries);
      } catch {
        if (!cancelled) setLedger([]);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repo, window]);

  if (!data && !error) {
    return (
      <section data-testid="briefing-loading" role="status">
        {slowLoad ? 'Still generating — first loads can take up to 2 minutes…' : 'Loading…'}
      </section>
    );
  }

  return (
    <section data-testid="briefing-view">
      <header className="page-head">
        <div>
          <h2>Briefing</h2>
          <RepoLabel repo={data?.repo || repo} />
        </div>
        {error && (
          <div className="actions">
            <button type="button" data-testid="briefing-retry" onClick={() => void load()}>
              Retry
            </button>
          </div>
        )}
      </header>
      {/* Set-once settings live behind chrome (#839): the
          content owns the top of the view, not the knobs. */}
      <details data-testid="briefing-settings">
        <summary className="details-title">Digest &amp; notification settings</summary>
        <DigestControls />
        <PrefsControls />
      </details>
      {error && (
        <div data-testid="briefing-error" role="alert">
          Briefing unavailable — {error}
        </div>
      )}
      {data && (
        <BriefingBody
          data={data}
          ledger={ledger}
          onAskAgent={onAskAgent}
          onOpenHistory={onOpenHistory}
          onOpenReview={onOpenReview}
          window={window}
          onWindowChange={setWindow}
          onGenerate={() => void generate()}
          generating={genBase !== null}
          genError={genError}
        />
      )}
    </section>
  );
}

export function BriefingBody({
  data,
  ledger,
  onAskAgent,
  onOpenHistory,
  onOpenReview,
  window = '24h',
  onWindowChange = () => {},
  onGenerate = () => {},
  generating = false,
  genError = null,
}: {
  data: BriefingResponse;
  ledger: LedgerEntry[] | null;
  onAskAgent: (seed: string) => void;
  onOpenHistory: () => void;
  onOpenReview: (prNumber: string) => void;
  window?: ActivityWindow;
  onWindowChange?: (window: ActivityWindow) => void;
  onGenerate?: () => void;
  generating?: boolean;
  genError?: string | null;
}) {
  const actions = briefingActions(data);
  const nowSec = Date.now() / 1000;
  const inWindow = inActivityWindow(ledger ?? [], window, nowSec);
  const highlights = toHistoryRows([], inWindow).slice(0, 3);
  const freshness = briefingFreshness(data);
  const prBranches = data.prs
    .map((pr) => String(pr.branch ?? '').trim())
    .filter((b) => b.length > 0);
  const board = visibleBoardItems(data.board ?? [], prBranches);
  const hasNarrative = typeof data.narrative === 'string' && data.narrative.trim() !== '';
  // The lead pairs the narrative/summary with its action; a bare
  // all-clear (no narrative, one fallback seed) keeps the classic
  // Next-actions list with nothing hoisted.
  const showLead = actions.length > 1 || hasNarrative;
  return (
    <>
      <p className="row-meta" data-testid="briefing-summary">
        {briefingSummary(data)}
      </p>
      {freshness && (
        <p className="row-meta" data-testid="briefing-freshness">
          {freshness}
        </p>
      )}
      {hasNarrative && (
        <p className="briefing-narrative" data-testid="briefing-narrative">
          {data.narrative}
        </p>
      )}
      {/* The lead names the top risk; its action rides inline
          (#839) instead of waiting in Next actions below. */}
      {showLead && (
        <div className="actions" data-testid="briefing-lead-action">
          <button type="button" className="btn-primary" onClick={() => onAskAgent(actions[0].seed)}>
            {actions[0].label}
          </button>
          {data.workflow_lines.length > 0 && (
            <button type="button" className="btn-secondary" onClick={onOpenHistory}>
              View failures in History
            </button>
          )}
        </div>
      )}
      {data.prs_err && (
        <p className="row-meta" role="alert">
          {data.prs_err}
        </p>
      )}
      {briefingAllClear(data) ? (
        <div data-testid="briefing-clear">
          <h3>All clear</h3>
          <p className="row-meta">No open PRs, no conflicts, no failing runs.</p>
          <div className="actions">
            <button
              type="button"
              data-testid="briefing-generate"
              className="btn-secondary"
              disabled={generating}
              onClick={onGenerate}
            >
              {generating ? 'Generating briefing…' : 'Generate Briefing Now'}
            </button>
          </div>
          {generating && (
            <p className="row-meta" role="status" data-testid="briefing-generating">
              Generating a fresh briefing — this can take a minute…
            </p>
          )}
          {genError && (
            <p className="row-meta" role="alert" data-testid="briefing-generate-error">
              {genError}
            </p>
          )}
        </div>
      ) : (
        <>
          <Section title="Needs attention" lines={data.attention} />
          {data.prs.length > 0 && (
            <section>
              <h3>Open pull requests</h3>
              <ul className="session-list" data-testid="briefing-pipeline">
                {data.prs.map((pr, i) => (
                  // eslint-disable-next-line react/no-array-index-key
                  <PrRow
                    key={i}
                    pr={pr}
                    repo={data.repo}
                    board={data.board ?? []}
                    reviewsLines={data.reviews_lines}
                    onOpenReview={onOpenReview}
                    onAskAgent={onAskAgent}
                  />
                ))}
              </ul>
              {typeof data.enrichment_note === 'string' && data.enrichment_note.trim() !== '' && (
                <p className="row-meta" data-testid="briefing-enrichment-note">
                  Note: {data.enrichment_note}.
                </p>
              )}
            </section>
          )}
          <Section title="Reviews" lines={data.reviews_lines} err={reviewsErrText(data.reviews_err)} />
          <BoardSection board={board} lines={data.board_lines} err={data.board_err} onOpenReview={onOpenReview} />
          <Section title="Conflicts" lines={data.conflicts_lines} err={data.conf_err} />
          <Section title="Overlaps" lines={data.overlaps_lines} />
          <RepairPicker repo={data.repo} prs={data.prs} onAskAgent={onAskAgent} />
          {(data.workflow_lines.length > 0 || data.runs_err) && (
            <section>
              <h3>Failing runs</h3>
              {data.runs_err && (
                <p className="row-meta" role="alert">
                  {data.runs_err}
                </p>
              )}
              <GroupedLines
                lines={data.workflow_lines}
                onInvestigate={(g) =>
                  onAskAgent(
                    `Investigate this repeated console failure (${g.count}×) in ${data.repo}: ${cleanBriefingLine(g.text)}`,
                  )
                }
              />
            </section>
          )}
        </>
      )}
      <PlansSection repo={data.repo} onAskAgent={onAskAgent} />
      <section aria-label="Next actions">
        <h3>Next actions</h3>
        {/* The top action already rides inline under the lead; the
            section keeps the remainder so no seed renders twice.
            Without a lead the full list renders, as before. */}
        {showLead && actions.length <= 1 ? (
          <p className="row-meta">The top action is above, beside the summary.</p>
        ) : (
          <div className="actions" data-testid="briefing-actions">
            {(showLead ? actions.slice(1) : actions).map((a, i) => (
              <button
                key={a.label}
                type="button"
                className={showLead || i > 0 ? 'btn-secondary' : 'btn-primary'}
                onClick={() => onAskAgent(a.seed)}
              >
                {a.label}
              </button>
            ))}
          </div>
        )}
      </section>
      <section aria-label="Recent activity">
        <h3>Recent activity</h3>
        <div
          className="actions"
          role="group"
          aria-label="Activity window"
          data-testid="briefing-activity-window"
        >
          {ACTIVITY_WINDOWS.map((o) => (
            <button
              key={o.value}
              type="button"
              data-testid={`briefing-window-${o.value}`}
              aria-pressed={window === o.value}
              onClick={() => onWindowChange(o.value)}
            >
              {o.label}
            </button>
          ))}
        </div>
        {highlights.length > 0 ? (
          <ul className="session-list" data-testid="briefing-highlights">
            {highlights.map((r) => (
              <li key={r.key} className="detail">
                <span className="row-title">{r.summary}</span>
                <p className="row-meta">
                  {r.kind} · {r.at}
                </p>
              </li>
            ))}
          </ul>
        ) : (
          <p className="row-meta">No recent activity to show.</p>
        )}
        <p className="row-meta" data-testid="briefing-trend">
          {dayTrend(inWindow, nowSec, window)}
        </p>
        <div className="actions">
          <button type="button" data-testid="briefing-history" onClick={onOpenHistory}>
            Full log in History
          </button>
        </div>
      </section>
    </>
  );
}
