import { useEffect, useRef, useState } from 'react';
import { fetchBriefing, fetchDigestSchedule, fetchLedger, fetchNotificationPreferences, fetchNotificationStatus, saveNotificationPreferences, setDigestSchedule, triggerDigest } from '../apiClient';
import type { DigestChannelResult, DigestSchedule, NotificationPreferences, NotificationsStatus } from '../apiClient';
import type { BoardItem, BriefingResponse, LedgerEntry } from '../apiTypes';
import { isHistoryFailure, toHistoryRows } from './historyUtils';
import type { HistoryRow } from './historyUtils';
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

function BoardSection({
  board,
  lines,
  err,
}: {
  board: BoardItem[];
  lines: string[];
  err?: string;
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
                {chips.map((c) => (
                  <span key={c} className="repo-chip" title={c.length > 32 ? c : undefined}>
                    {truncateChip(c)}
                  </span>
                ))}
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
  const held = sched.held_until ? ` · held until ${formatDigestStamp(sched.held_until)}` : '';
  if (!res.at) return `${next}${held}.`;
  if (res.ok) return `${next} · last sent ${formatDigestStamp(res.at)}${held}.`;
  const err = res.error || 'unknown error';
  const short = err.length > 120 ? `${err.slice(0, 120)}…` : err;
  return `${next} · last scheduled send failed (${short})${held}.`;
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

// Notification preferences + quiet hours (issue #615's digest
// cluster, issue #656). One prefs object, one round trip.
export function prefsStatusLine(prefs: NotificationPreferences): string {
  const muted = prefs.muted_events.length > 0
    ? `Muted: ${prefs.muted_events.join(', ')}`
    : 'All events on';
  const quiet = prefs.quiet_hours
    ? `Quiet ${prefs.quiet_hours.start}–${prefs.quiet_hours.end}`
    : 'No quiet hours';
  const held = prefs.held_until
    ? ` · Digest held until ${formatDigestStamp(prefs.held_until)}`
    : '';
  return `${muted} · ${quiet}${held}.`;
}

export function PrefsControlsView({
  prefs,
  prefsErr,
  busy,
  draftStart,
  draftEnd,
  onToggle,
  onDraft,
  onSaveQuiet,
  onClearQuiet,
}: {
  prefs: NotificationPreferences | null;
  prefsErr: string | null;
  busy: boolean;
  draftStart: string;
  draftEnd: string;
  onToggle: (event: string) => void;
  onDraft: (start: string, end: string) => void;
  onSaveQuiet: () => void;
  onClearQuiet: () => void;
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
      <div className="actions">
        <label className="row-meta" htmlFor="briefing-quiet-start">
          Quiet from{' '}
          <input
            id="briefing-quiet-start"
            data-testid="briefing-quiet-start"
            type="time"
            value={draftStart}
            disabled={busy || !prefs}
            onChange={(ev) => onDraft(ev.target.value, draftEnd)}
          />
        </label>
        <label className="row-meta" htmlFor="briefing-quiet-end">
          to{' '}
          <input
            id="briefing-quiet-end"
            data-testid="briefing-quiet-end"
            type="time"
            value={draftEnd}
            disabled={busy || !prefs}
            onChange={(ev) => onDraft(draftStart, ev.target.value)}
          />
        </label>
        <button
          type="button"
          data-testid="briefing-quiet-save"
          title="Save quiet hours (server-local time; overnight windows allowed)"
          disabled={busy || !prefs}
          onClick={onSaveQuiet}
        >
          Save quiet hours
        </button>
        <button
          type="button"
          data-testid="briefing-quiet-clear"
          title="Clear quiet hours"
          disabled={busy || !prefs || !prefs?.quiet_hours}
          onClick={onClearQuiet}
        >
          Clear
        </button>
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
  const [draftStart, setDraftStart] = useState('');
  const [draftEnd, setDraftEnd] = useState('');
  const draftsInit = useRef(false);

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

  // Seed the time inputs from saved quiet hours once; later prefs
  // refreshes (e.g. after a mute toggle) must not clobber typing.
  useEffect(() => {
    if (!draftsInit.current && prefs?.quiet_hours) {
      setDraftStart(prefs.quiet_hours.start);
      setDraftEnd(prefs.quiet_hours.end);
      draftsInit.current = true;
    }
  }, [prefs]);

  async function save(body: { muted_events?: string[]; quiet_hours?: { start: string; end: string } | null }) {
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
      draftStart={draftStart}
      draftEnd={draftEnd}
      onToggle={(e) => toggle(e)}
      onDraft={(s, e) => {
        setDraftStart(s);
        setDraftEnd(e);
      }}
      onSaveQuiet={() => void save({ quiet_hours: { start: draftStart, end: draftEnd } })}
      onClearQuiet={() => {
        setDraftStart('');
        setDraftEnd('');
        void save({ quiet_hours: null });
      }}
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

// Copy-as-text digest (issue #816): plain-text mirror of the
// rendered briefing in on-screen section order, empty sections
// omitted. Line-builder shape follows the chat transcript export;
// lines reuse cleanBriefingLine so the digest reads like the view.
export function briefingDigestText(data: BriefingResponse, activity: HistoryRow[]): string {
  const lines = [`# Briefing — ${data.repo}`, ''];
  lines.push(briefingSummary(data), '');
  if (typeof data.narrative === 'string' && data.narrative.trim() !== '') {
    lines.push(data.narrative.trim(), '');
  }
  function section(title: string, items: string[]) {
    if (items.length === 0) return;
    lines.push(`## ${title}`, '');
    for (const item of items) lines.push(`- ${item}`);
    lines.push('');
  }
  section('Needs attention', data.attention.map(cleanBriefingLine).filter((l) => l !== ''));
  section(
    'Open pull requests',
    data.prs.map((pr) => {
      const meta = [pr.author, pr.branch, pr.ciLabel ?? pr.ciStatus]
        .map((v) => String(v ?? '').trim())
        .filter((v) => v.length > 0);
      const head = `#${String(pr.number ?? '?')} ${String(pr.title ?? 'Untitled')}`;
      return meta.length > 0 ? `${head} (${meta.join(' · ')})` : head;
    }),
  );
  const reviewItems = [
    ...(reviewsErrText(data.reviews_err) ? [`Unavailable: ${reviewsErrText(data.reviews_err)}`] : []),
    ...data.reviews_lines.map(cleanBriefingLine).filter((l) => l !== ''),
  ];
  section('Reviews', reviewItems);
  // Board mirrors BoardSection: structured chips win when present,
  // full text kept (no chip truncation) for a shareable digest.
  const boardItems =
    (data.board ?? []).length > 0
      ? (data.board ?? []).map((b) =>
          [b.branch, b.issue ? `issue ${b.issue}` : '', b.status]
            .map((v) => (v ?? '').trim())
            .filter((v) => v.length > 0 && v !== '?')
            .join(' · '),
        ).filter((l) => l !== '')
      : data.board_lines.map(cleanBriefingLine).filter((l) => l !== '');
  if (data.board_err) boardItems.unshift(`Unavailable: ${data.board_err}`);
  section('Board', boardItems);
  const conflictItems = [
    ...(data.conf_err ? [`Unavailable: ${data.conf_err}`] : []),
    ...data.conflicts_lines.map(cleanBriefingLine).filter((l) => l !== ''),
  ];
  section('Conflicts', conflictItems);
  section('Overlaps', data.overlaps_lines.map(cleanBriefingLine).filter((l) => l !== ''));
  const runItems = [
    ...(data.runs_err ? [`Unavailable: ${data.runs_err}`] : []),
    ...data.workflow_lines.map(cleanBriefingLine).filter((l) => l !== ''),
  ];
  section('Failing runs', runItems);
  section('Recent activity', activity.map((r) => `${r.summary} (${r.kind} · ${r.at})`));
  return lines.join('\n').replace(/\n+$/, '\n');
}

export function CopyDigestButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState<string | null>(null);
  async function onCopy() {
    setCopyError(null);
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch (err) {
      setCopied(false);
      setCopyError(err instanceof Error ? err.message : String(err));
    }
  }
  return (
    <div className="actions">
      <button type="button" data-testid="briefing-copy" onClick={() => void onCopy()}>
        Copy briefing
      </button>
      {copied && (
        <span className="row-meta" data-testid="briefing-copied" role="status">
          Copied.
        </span>
      )}
      {copyError && (
        <span className="row-meta" data-testid="briefing-copy-error" role="alert">
          Couldn&apos;t copy: {copyError}
        </span>
      )}
    </div>
  );
}

function PrRow({ pr }: { pr: Record<string, unknown> }) {
  const meta = [pr.author, pr.branch, pr.ciLabel ?? pr.ciStatus]
    .map((v) => String(v ?? '').trim())
    .filter((v) => v.length > 0);
  return (
    <li className="detail">
      <span className="row-title">
        #{String(pr.number ?? '?')} {String(pr.title ?? 'Untitled')}
      </span>
      {meta.length > 0 && <p className="row-meta">{meta.join(' · ')}</p>}
    </li>
  );
}

export default function BriefingView({
  repo,
  onAskAgent,
  onOpenHistory,
  reloadRequest = 0,
}: {
  repo: string;
  onAskAgent: (seed: string) => void;
  onOpenHistory: () => void;
  // Palette request (issue #810): a nonce bump re-fetches the
  // briefing while mounted. Mount already loads, so the opening
  // nonce is adopted, not acted on — only later bumps reload.
  reloadRequest?: number;
}) {
  const [data, setData] = useState<BriefingResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ledger, setLedger] = useState<LedgerEntry[] | null>(null);
  const [window, setWindow] = useState<ActivityWindow>('24h');

  async function load() {
    setError(null);
    try {
      setData(await fetchBriefing(repo));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
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

  if (!data && !error) return <section data-testid="briefing-loading" role="status">Loading…</section>;

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
      <DigestControls />
      <PrefsControls />
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
          window={window}
          onWindowChange={setWindow}
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
  window = '24h',
  onWindowChange = () => {},
}: {
  data: BriefingResponse;
  ledger: LedgerEntry[] | null;
  onAskAgent: (seed: string) => void;
  onOpenHistory: () => void;
  window?: ActivityWindow;
  onWindowChange?: (window: ActivityWindow) => void;
}) {
  const actions = briefingActions(data);
  const nowSec = Date.now() / 1000;
  const inWindow = inActivityWindow(ledger ?? [], window, nowSec);
  const highlights = toHistoryRows([], inWindow).slice(0, 3);
  const freshness = briefingFreshness(data);
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
      <CopyDigestButton text={briefingDigestText(data, highlights)} />
      {typeof data.narrative === 'string' && data.narrative.trim() !== '' && (
        <p className="briefing-narrative" data-testid="briefing-narrative">
          {data.narrative}
        </p>
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
        </div>
      ) : (
        <>
          <Section title="Needs attention" lines={data.attention} />
          {data.prs.length > 0 && (
            <section>
              <h3>Open pull requests</h3>
              <ul className="session-list">
                {data.prs.map((pr, i) => (
                  // eslint-disable-next-line react/no-array-index-key
                  <PrRow key={i} pr={pr} />
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
          <BoardSection board={data.board ?? []} lines={data.board_lines} err={data.board_err} />
          <Section title="Conflicts" lines={data.conflicts_lines} err={data.conf_err} />
          <Section title="Overlaps" lines={data.overlaps_lines} />
          <Section title="Failing runs" lines={data.workflow_lines} err={data.runs_err} />
        </>
      )}
      <section aria-label="Next actions">
        <h3>Next actions</h3>
        <div className="actions" data-testid="briefing-actions">
          {actions.map((a) => (
            <button key={a.label} type="button" onClick={() => onAskAgent(a.seed)}>
              {a.label}
            </button>
          ))}
        </div>
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
