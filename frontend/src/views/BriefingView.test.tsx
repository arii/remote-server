import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import type { DigestChannelResult, DigestSchedule, NotificationPreferences, NotificationsStatus } from '../apiClient';
import type { BriefingResponse, LedgerEntry } from '../apiTypes';
import {
  ACTIVITY_WINDOWS,
  activityWindowSeconds,
  BriefingBody,
  dayTrend,
  DigestControlsView,
  digestChannels,
  digestDisabledReason,
  digestResultLine,
  digestScheduleLine,
  digestStatusLine,
  inActivityWindow,
  PrefsControlsView,
  prefsStatusLine,
} from './BriefingView';

function status(over: Partial<NotificationsStatus> = {}): NotificationsStatus {
  return {
    ntfy_configured: false,
    topic: '',
    webhook_configured: false,
    digest_interval_hours: 12,
    last_digest: 0,
    enabled_events: [],
    ...over,
  };
}

describe('digest channels (issue #615)', () => {
  it('lists configured channels only', () => {
    expect(digestChannels(status())).toEqual([]);
    expect(digestChannels(status({ ntfy_configured: true }))).toEqual(['ntfy']);
    expect(digestChannels(status({ webhook_configured: true }))).toEqual(['webhook']);
    expect(
      digestChannels(status({ ntfy_configured: true, webhook_configured: true })),
    ).toEqual(['ntfy', 'webhook']);
  });
});

describe('digest disabled reason (issue #615)', () => {
  it('disables while loading, failed, or running', () => {
    expect(digestDisabledReason(null, null, false)).toContain('Loading');
    expect(digestDisabledReason(null, 'boom', false)).toContain('unavailable');
    expect(
      digestDisabledReason(status({ ntfy_configured: true }), null, true),
    ).toContain('Sending');
  });

  it('names the missing configuration when unconfigured', () => {
    const reason = digestDisabledReason(status(), null, false);
    expect(reason).toContain('NTFY_TOPIC');
    expect(reason).toContain('NOTIFICATION_WEBHOOK_URL');
    expect(reason).toContain('docs/buttons.md');
  });

  it('enables when a channel is configured', () => {
    expect(
      digestDisabledReason(status({ ntfy_configured: true }), null, false),
    ).toBeNull();
    expect(
      digestDisabledReason(status({ webhook_configured: true }), null, false),
    ).toBeNull();
  });
});

describe('digest status line (issue #615)', () => {
  it('shows last digest and channels when configured', () => {
    const line = digestStatusLine(
      status({ ntfy_configured: true, last_digest: 1793760000 }),
    );
    expect(line).toContain('Last digest');
    expect(line).toContain('ntfy');
  });

  it('says never when no digest was sent', () => {
    expect(
      digestStatusLine(status({ webhook_configured: true, last_digest: 0 })),
    ).toContain('No digest sent yet');
  });

  it('shows the unconfigured reason instead of a fake state', () => {
    expect(digestStatusLine(status())).toContain('not configured');
  });
});

describe('digest result line (issue #615)', () => {
  it('never reports success with no channel results', () => {
    const line = digestResultLine({}, 'now');
    expect(line).toContain('delivered nowhere');
    expect(line).not.toContain('sent ');
  });

  it('confirms send with timestamp and channels on full success', () => {
    const line = digestResultLine(
      { ntfy: { ok: true, status: 200 }, webhook: { ok: true, status: 200 } },
      'STAMP',
    );
    expect(line).toContain('Digest sent STAMP');
    expect(line).toContain('ntfy');
    expect(line).toContain('webhook');
  });

  it('reports failure with channel errors, not a stamp', () => {
    const line = digestResultLine({ ntfy: { ok: false, error: 'refused' } }, 'STAMP');
    expect(line).toContain('Digest failed');
    expect(line).toContain('refused');
  });

  it('reports partial sends honestly', () => {
    const line = digestResultLine(
      { ntfy: { ok: true }, webhook: { ok: false, error: 'down' } },
      'STAMP',
    );
    expect(line).toContain('partially sent');
    expect(line).toContain('ntfy ok');
    expect(line).toContain('webhook failed (down)');
  });
});

describe('digest controls view (issue #615)', () => {
  const noop = () => {};

  it('renders an enabled run button when configured', () => {
    const html = renderToStaticMarkup(
      <DigestControlsView
        status={status({ ntfy_configured: true, last_digest: 1793760000 })}
        statusErr={null}
        running={false}
        result={null}
        onRun={noop}
      />,
    );
    expect(html).toContain('data-testid="briefing-run-now"');
    expect(html).toContain('Run briefing now');
    expect(html).not.toContain('disabled');
    expect(html).toContain('data-testid="briefing-digest-status"');
    expect(html).toContain('Last digest');
  });

  it('disables with the reason when unconfigured', () => {
    const html = renderToStaticMarkup(
      <DigestControlsView status={status()} statusErr={null} running={false} result={null} onRun={noop} />,
    );
    expect(html).toContain('disabled');
    expect(html).toContain('NTFY_TOPIC');
    expect(html).toContain('NOTIFICATION_WEBHOOK_URL');
  });

  it('shows sending state while a run is in flight', () => {
    const html = renderToStaticMarkup(
      <DigestControlsView
        status={status({ ntfy_configured: true })}
        statusErr={null}
        running
        result={null}
        onRun={noop}
      />,
    );
    expect(html).toContain('disabled');
    expect(html).toContain('Sending…');
  });

  it('renders the result line after a run', () => {
    const html = renderToStaticMarkup(
      <DigestControlsView
        status={status({ ntfy_configured: true })}
        statusErr={null}
        running={false}
        result="Digest sent X via ntfy."
        onRun={noop}
      />,
    );
    expect(html).toContain('data-testid="briefing-digest-result"');
    expect(html).toContain('Digest sent X via ntfy.');
  });

  it('surfaces status fetch errors without a fake state', () => {
    const html = renderToStaticMarkup(
      <DigestControlsView status={null} statusErr="boom" running={false} result={null} onRun={noop} />,
    );
    expect(html).toContain('disabled');
    expect(html).toContain('Digest status unavailable — boom');
  });
});

describe('activity window (issue #609)', () => {
  const now = 1793745800;
  const entries = [
    { kind: 'chat', event: 'a', ts: now - 100, ok: true },
    { kind: 'webhook', event: 'b', ts: now - 4000, ok: false },
    { kind: 'webhook', event: 'c', ts: now - 100000, ok: false },
    { kind: 'webhook', event: 'd', ts: now - 500000, ok: true },
  ] as LedgerEntry[];

  it('offers 1h/24h/7d presets defaulting to 24h', () => {
    expect(ACTIVITY_WINDOWS.map((o) => o.value)).toEqual(['1h', '24h', '7d']);
    expect(activityWindowSeconds('1h')).toBe(3600);
    expect(activityWindowSeconds('24h')).toBe(86400);
    expect(activityWindowSeconds('7d')).toBe(604800);
  });

  it('narrows entries to the selected window', () => {
    expect(inActivityWindow(entries, '1h', now).map((e) => e.event)).toEqual(['a']);
    expect(inActivityWindow(entries, '24h', now).map((e) => e.event)).toEqual(['a', 'b']);
    expect(inActivityWindow(entries, '7d', now).map((e) => e.event)).toEqual([
      'a',
      'b',
      'c',
      'd',
    ]);
  });

  it('labels trend counts by window', () => {
    expect(dayTrend(entries, now, '1h')).toBe('Last 1h: 1 event, 0 failures.');
    expect(dayTrend(entries, now)).toBe('Last 24h: 2 events, 1 failure.');
    expect(dayTrend(entries, now, '7d')).toBe('Last 7d: 4 events, 2 failures.');
  });

  it('renders the window selector with the active window pressed', () => {
    const empty = {
      repo: 'r',
      attention: [],
      prs: [],
      prs_err: '',
      reviews_lines: [],
      board: [],
      board_lines: [],
      board_err: '',
      conflicts_lines: [],
      overlaps_lines: [],
      failing_runs: [],
      workflow_lines: [],
      runs_err: '',
    } as BriefingResponse;
    const noop = () => {};
    // Component filters against the real clock: anchor fixtures to now.
    const liveNow = Math.floor(Date.now() / 1000);
    const liveEntries = [
      { kind: 'chat', event: 'a', ts: liveNow - 100, ok: true },
      { kind: 'webhook', event: 'b', ts: liveNow - 4000, ok: false },
    ] as LedgerEntry[];
    const html = renderToStaticMarkup(
      <BriefingBody
        data={empty}
        ledger={liveEntries}
        onAskAgent={noop}
        onOpenHistory={noop}
        window="1h"
        onWindowChange={noop}
      />,
    );
    expect(html).toContain('data-testid="briefing-activity-window"');
    expect(html).toContain('data-testid="briefing-window-1h"');
    expect(html).toContain('data-testid="briefing-window-24h"');
    expect(html).toContain('data-testid="briefing-window-7d"');
    expect(html).toContain('aria-pressed="true"');
    expect(html).toContain('data-testid="briefing-trend"');
    expect(html).toContain('Last 1h: 1 event, 0 failures.');
  });
});

describe('digest schedule line (issue #636)', () => {
  function sched(over: Partial<DigestSchedule> = {}): DigestSchedule {
    return { cadence: 'off', next_run: null, last_sent: null, last_result: {}, held_until: null, ...over };
  }

  it('reports off plainly', () => {
    expect(digestScheduleLine(sched())).toBe('Scheduled digests off.');
  });

  it('shows next run with last-sent and failure detail', () => {
    const next = digestScheduleLine(sched({ cadence: 'daily', next_run: 1793760000 }));
    expect(next).toContain('Next digest');
    expect(next).not.toContain('off');
    const sent = digestScheduleLine(
      sched({ cadence: 'weekly', next_run: 1793760000, last_result: { ok: true, at: 1793673600 } }),
    );
    expect(sent).toContain('last sent');
    const failed = digestScheduleLine(
      sched({
        cadence: 'daily',
        next_run: 1793760000,
        last_result: { ok: false, at: 1793673600, error: 'boom' },
      }),
    );
    expect(failed).toContain('last scheduled send failed (boom)');
  });
});

describe('digest schedule control (issue #636)', () => {
  const noop = () => {};

  function view(sched: DigestSchedule | null) {
    return renderToStaticMarkup(
      <DigestControlsView
        status={status({ ntfy_configured: true })}
        statusErr={null}
        running={false}
        result={null}
        onRun={noop}
        sched={sched}
        schedErr={null}
        schedBusy={false}
        onSetCadence={noop}
      />,
    );
  }

  it('renders Daily/Weekly/Off with the current cadence pressed', () => {
    const html = view({ cadence: 'weekly', next_run: 1793760000, last_sent: null, last_result: {}, held_until: null });
    expect(html).toContain('data-testid="briefing-digest-schedule"');
    expect(html).toContain('data-testid="briefing-schedule-daily"');
    expect(html).toContain('data-testid="briefing-schedule-weekly"');
    expect(html).toContain('data-testid="briefing-schedule-off"');
    expect(html).toContain('aria-pressed="true"');
    expect(html).toContain('data-testid="briefing-digest-schedule-status"');
    expect(html).toContain('Next digest');
  });

  it('keeps the manual run button behavior unchanged', () => {
    const html = view(null);
    expect(html).toContain('data-testid="briefing-run-now"');
    expect(html).toContain('Run briefing now');
  });
});

describe('notification preferences (issue #656)', () => {
  function prefs(over: Partial<NotificationPreferences> = {}): NotificationPreferences {
    return { muted_events: [], quiet_hours: null, events: ['ci_fail', 'conflict', 'digest'], held_until: null, ...over };
  }

  it('summarizes muted + quiet + held in one line', () => {
    expect(prefsStatusLine(prefs())).toBe('All events on · No quiet hours.');
    expect(prefsStatusLine(prefs({ muted_events: ['ci_fail'] }))).toContain('Muted: ci_fail');
    expect(prefsStatusLine(prefs({ quiet_hours: { start: '22:00', end: '07:00' } }))).toContain(
      'Quiet 22:00–07:00',
    );
    expect(prefsStatusLine(prefs({ held_until: 1793760000 }))).toContain('Digest held until');
  });

  it('renders event chips pressed by muted state', () => {
    const noop = () => {};
    const html = renderToStaticMarkup(
      <PrefsControlsView
        prefs={prefs({ muted_events: ['ci_fail'], quiet_hours: { start: '22:00', end: '07:00' } })}
        prefsErr={null}
        busy={false}
        draftStart="22:00"
        draftEnd="07:00"
        onToggle={noop}
        onDraft={noop}
        onSaveQuiet={noop}
        onClearQuiet={noop}
      />,
    );
    expect(html).toContain('data-testid="briefing-prefs-events"');
    expect(html).toContain('data-testid="briefing-prefs-mute-ci_fail"');
    expect(html).toContain('data-testid="briefing-prefs-mute-conflict"');
    expect(html).toContain('data-testid="briefing-prefs-mute-digest"');
    expect(html).toContain('aria-pressed="true"');
    expect(html).toContain('aria-pressed="false"');
    expect(html).toContain('data-testid="briefing-quiet-start"');
    expect(html).toContain('data-testid="briefing-quiet-end"');
    expect(html).toContain('value="22:00"');
    expect(html).toContain('data-testid="briefing-quiet-save"');
    expect(html).toContain('data-testid="briefing-quiet-clear"');
    expect(html).toContain('data-testid="briefing-prefs-status"');
    expect(html).toContain('Muted: ci_fail');
    expect(html).not.toContain('disabled');
  });

  it('disables while busy and surfaces errors', () => {
    const noop = () => {};
    const busy = renderToStaticMarkup(
      <PrefsControlsView prefs={prefs()} prefsErr={null} busy draftStart="" draftEnd="" onToggle={noop} onDraft={noop} onSaveQuiet={noop} onClearQuiet={noop} />,
    );
    expect(busy).toContain('disabled');
    const failed = renderToStaticMarkup(
      <PrefsControlsView prefs={null} prefsErr="boom" busy={false} draftStart="" draftEnd="" onToggle={noop} onDraft={noop} onSaveQuiet={noop} onClearQuiet={noop} />,
    );
    expect(failed).toContain('Notification preferences unavailable — boom');
  });

  it('reports suppressed digests honestly, never as sent', () => {
    const line = digestResultLine(
      { suppressed: 'quiet hours until 07:00' } as unknown as Record<string, DigestChannelResult>,
      'STAMP',
    );
    expect(line).toBe('Digest not sent — quiet hours until 07:00.');
    expect(line).not.toContain('sent STAMP');
  });

  it('shows held digests in the schedule line', () => {
    const sched = (over: Partial<DigestSchedule> = {}): DigestSchedule => ({
      cadence: 'off',
      next_run: null,
      last_sent: null,
      last_result: {},
      held_until: null,
      ...over,
    });
    expect(
      digestScheduleLine(sched({ cadence: 'daily', next_run: 1793760000, held_until: 1793742000 })),
    ).toContain('held until');
    expect(digestScheduleLine(sched())).not.toContain('held until');
  });
});
