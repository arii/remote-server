import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import {
  ActionControl,
  askAgentSeed,
  reconcileSnoozed,
  RepoTag,
  resolveAttentionTarget,
  showRepoTag,
  SnoozedStub,
  SNOOZE_PRESETS,
  SnoozeButtons,
  snoozedRows,
  snoozePresetLabel,
} from './NowView';
import { ALL_REPOS, isRollupRepo } from '../components/RepoPicker';
import type { AttentionItem } from '../attentionTypes';

describe('snooze presets (issue #614)', () => {
  it('offers 1h and 8h', () => {
    expect(SNOOZE_PRESETS).toEqual([60, 480]);
    expect(snoozePresetLabel(60)).toBe('Snooze 1h');
    expect(snoozePresetLabel(480)).toBe('Snooze 8h');
  });
});

describe('reconcileSnoozed (issue #614)', () => {
  const entry = { until: 2000, title: 't' };

  it('keeps live stubs missing from the list', () => {
    expect(reconcileSnoozed({ a: entry }, [], 1000)).toEqual({ a: entry });
  });

  it('drops expired stubs so items resurface', () => {
    expect(reconcileSnoozed({ a: entry }, [], 2000)).toEqual({});
    expect(reconcileSnoozed({ a: entry }, [], 3000)).toEqual({});
  });

  it('drops stubs whose item is back in the list', () => {
    expect(reconcileSnoozed({ a: entry }, ['a'], 1000)).toEqual({});
  });
});

describe('snoozedRows (issue #614)', () => {
  it('returns only stubs missing from the live list', () => {
    const snoozed = {
      a: { until: 9, title: 'A' },
      b: { until: 9, title: 'B' },
    };
    expect(snoozedRows(snoozed, ['b', 'c'])).toEqual([['a', snoozed.a]]);
  });
});

describe('SnoozeButtons (issue #614)', () => {
  it('renders one button per preset with stable testids', () => {
    const html = renderToStaticMarkup(
      <SnoozeButtons itemId="job-j1" busy={false} onSnooze={() => {}} />,
    );
    expect(html).toContain('data-testid="snooze-job-j1-60"');
    expect(html).toContain('data-testid="snooze-job-j1-480"');
    expect(html).toContain('Snooze 1h');
    expect(html).toContain('Snooze 8h');
    expect(html).not.toContain('disabled');
  });

  it('disables while a snooze is in flight', () => {
    const html = renderToStaticMarkup(
      <SnoozeButtons itemId="job-j1" busy onSnooze={() => {}} />,
    );
    expect(html).toContain('disabled');
  });
});

describe('SnoozedStub (issue #614)', () => {
  it('collapses the card with an undo affordance', () => {
    const html = renderToStaticMarkup(
      <SnoozedStub itemId="job-j1" title="Failed job" until={1793760000} busy={false} onUnsnooze={() => {}} />,
    );
    expect(html).toContain('data-testid="now-snoozed-job-j1"');
    expect(html).toContain('Snoozed: Failed job');
    expect(html).toContain('Hidden until');
    expect(html).toContain('data-testid="unsnooze-job-j1"');
    expect(html).toContain('Unsnooze');
    expect(html).not.toContain('disabled');
  });

  it('disables undo while unsnoozing', () => {
    const html = renderToStaticMarkup(
      <SnoozedStub itemId="job-j1" title="Failed job" until={1793760000} busy onUnsnooze={() => {}} />,
    );
    expect(html).toContain('disabled');
    expect(html).toContain('Unsnoozing…');
  });
});

describe('rollup repo (issue #610)', () => {
  it('recognizes only the All repos pseudo-repo', () => {
    expect(ALL_REPOS).toBe('all');
    expect(isRollupRepo('all')).toBe(true);
    expect(isRollupRepo('remote-server')).toBe(false);
    expect(isRollupRepo('')).toBe(false);
  });

  it('RepoTag renders a stable per-repo chip', () => {
    const html = renderToStaticMarkup(<RepoTag repo="boomtick" />);
    expect(html).toContain('data-testid="now-repo-boomtick"');
    expect(html).toContain('repo-chip');
    expect(html).toContain('boomtick');
  });
});

describe('single-repo scoping (issue #723)', () => {
  it('tags rollup rows and foreign single-mode rows only', () => {
    expect(showRepoTag(true, 'boomtick', 'all')).toBe(true);
    expect(showRepoTag(true, 'remote-server', 'all')).toBe(true);
    expect(showRepoTag(false, 'boomtick', 'boomtick')).toBe(false);
    expect(showRepoTag(false, 'remote-server', 'boomtick')).toBe(true);
  });

  it('seeds Ask agent from the row repo, never the view repo', () => {
    const item: AttentionItem = {
      id: 'webhook-skip-1',
      kind: 'skipped-webhook',
      title: 'Webhook pull skipped — checkout lagging',
      detail: 'skipped: tree not ff-able',
      repo: 'remote-server',
      severity: 'info',
      action: { label: '', method: 'GET', href: '', destructive: false, intent: '' },
      created_at: 1,
    };
    expect(askAgentSeed(item)).toBe(
      'Webhook pull skipped — checkout lagging (remote-server): skipped: tree not ff-able',
    );
  });
});

describe('destructive POST actions (issue #508)', () => {
  const noop = () => {};
  function item(overrides: Partial<AttentionItem['action']>): AttentionItem {
    return {
      id: 'job-j1',
      kind: 'failed-job',
      title: 'Failed job',
      detail: 'd',
      repo: 'remote-server',
      severity: 'crit',
      action: { label: 'Cancel run', method: 'POST', href: 'api/jobs/j1/cancel', destructive: true, intent: 'cancel', ...overrides },
      created_at: 1,
    };
  }
  const handlers = { onDone: noop, onError: noop, onSection: noop, onTab: noop };

  it('uses tap-to-fire with an inline confirm, not hold-to-arm', () => {
    const html = renderToStaticMarkup(<ActionControl item={item({})} {...handlers} />);
    expect(html).toContain('data-testid="now-action-job-j1"');
    expect(html).toContain('asks for confirmation');
    expect(html).not.toContain('hold-disarmed');
    expect(html).not.toContain('Hold to arm');
  });

  it('keeps non-destructive POST actions as plain buttons', () => {
    const html = renderToStaticMarkup(
      <ActionControl item={item({ destructive: false })} {...handlers} />,
    );
    expect(html).not.toContain('tap-fire');
    expect(html).not.toContain('hold-disarmed');
  });

  it('keeps GET actions as plain buttons', () => {
    const html = renderToStaticMarkup(
      <ActionControl item={item({ method: 'GET', destructive: true })} {...handlers} />,
    );
    expect(html).not.toContain('tap-fire');
    expect(html).not.toContain('hold-disarmed');
  });
});

describe('attention action navigation (issue #464)', () => {
  it('maps every backend GET href to an in-app target', () => {
    expect(resolveAttentionTarget('api/jobs')).toBe('jobs');
    expect(resolveAttentionTarget('api/notifications/status')).toBe('briefing');
    expect(resolveAttentionTarget('api/prs?repo=remote-server')).toBe('review');
    expect(resolveAttentionTarget('api/workflow-health?repo=remote-server')).toBe('history');
    expect(resolveAttentionTarget('api/agents/budget')).toBe('chat');
  });

  it('tolerates leading slashes, query strings, and fragments', () => {
    expect(resolveAttentionTarget('/api/jobs')).toBe('jobs');
    expect(resolveAttentionTarget('/api/prs?repo=x#frag')).toBe('review');
    expect(resolveAttentionTarget('api/workflow-health?repo=x&other=1')).toBe('history');
  });

  it('returns null for unknown hrefs so they keep the fetch fallback', () => {
    expect(resolveAttentionTarget('api/unknown')).toBeNull();
    expect(resolveAttentionTarget('')).toBeNull();
    expect(resolveAttentionTarget('https://example.com/x')).toBeNull();
  });
});
