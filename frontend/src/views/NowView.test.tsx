import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import {
  ActionControl,
  askAgentSeed,
  DEPLOY_ACTION,
  heroCountsText,
  heroStatus,
  lastDeployAt,
  NowEmptyState,
  NowHero,
  filterBySeverity,
  reconcileSnoozed,
  RepoTag,
  resolveAttentionTarget,
  SeverityChips,
  SEVERITY_FILTERS,
  severityCounts,
  sevKey,
  showRepoTag,
  SnoozedStub,
  SNOOZE_PRESETS,
  SnoozeButtons,
  snoozedRows,
  snoozeAllInfoTargets,
  snoozePresetLabel,
  topUrgent,
} from './NowView';
import { ALL_REPOS, isRollupRepo } from '../components/RepoPicker';
import type { InboxFeed, SystemHealthResponse } from '../apiClient';
import type { JobRecord } from '../apiTypes';
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

describe('Now status hero (issue #811)', () => {
  function item(overrides: Partial<AttentionItem> = {}): AttentionItem {
    return {
      id: 'a1',
      kind: 'failed-job',
      title: 'Failed job',
      detail: 'd',
      repo: 'remote-server',
      severity: 'info',
      action: { label: 'View jobs', method: 'GET', href: 'api/jobs', destructive: false, intent: '' },
      created_at: 1,
      ...overrides,
    };
  }

  function health(overrides: Partial<SystemHealthResponse> = {}): SystemHealthResponse {
    return {
      disk: {},
      memory: {},
      containers: [{ name: 'c', service: 'c', state: 'running', status: 'up', health: 'healthy' }],
      containers_error: null,
      ...overrides,
    };
  }

  function feed(overrides: Partial<InboxFeed> = {}): InboxFeed {
    return {
      repo: 'remote-server',
      updated_at: 1,
      attention: [],
      merge_ready: { count: 0, top: [] },
      runs: { active: null, recent: [] },
      ...overrides,
    };
  }

  function job(overrides: Partial<JobRecord> = {}): JobRecord {
    return { id: 'j1', ...overrides };
  }

  it('heroStatus loads, clears, and escalates worst-first', () => {
    expect(heroStatus(null, [])).toBe('loading');
    expect(heroStatus(health(), [])).toBe('ok');
    expect(heroStatus(health(), [item({ severity: 'info' })])).toBe('ok');
    expect(heroStatus(health(), [item({ severity: 'warn' })])).toBe('warn');
    expect(heroStatus(health(), [item({ severity: 'crit' })])).toBe('crit');
    expect(heroStatus(health(), [item({ severity: 'warn' }), item({ id: 'a2', severity: 'crit' })])).toBe('crit');
  });

  it('heroStatus treats long severity spellings the same', () => {
    expect(heroStatus(health(), [item({ severity: 'critical' as AttentionItem['severity'] })])).toBe('crit');
    expect(heroStatus(health(), [item({ severity: 'warning' as AttentionItem['severity'] })])).toBe('warn');
  });

  it('heroStatus reads the stack: sick is crit, silent is warn', () => {
    expect(heroStatus(health({ containers_error: 'boom' }), [])).toBe('crit');
    expect(heroStatus(health({ containers: [{ state: 'exited' }] }), [])).toBe('crit');
    expect(heroStatus(health({ containers: [{ state: 'running', health: 'unhealthy' }] }), [])).toBe('crit');
    expect(heroStatus(health({ containers: [] }), [])).toBe('warn');
    // Stack trouble outranks a clear queue.
    expect(heroStatus(health({ containers_error: 'boom' }), [item()])).toBe('crit');
  });

  it('topUrgent returns null on an empty queue', () => {
    expect(topUrgent([])).toBeNull();
  });

  it('topUrgent picks worst severity, first on ties', () => {
    const info = item({ id: 'info', severity: 'info' });
    const warnA = item({ id: 'warn-a', severity: 'warn' });
    const warnB = item({ id: 'warn-b', severity: 'warn' });
    const crit = item({ id: 'crit', severity: 'crit' });
    expect(topUrgent([info, warnA, crit])).toBe(crit);
    expect(topUrgent([warnA, crit, info])).toBe(crit);
    expect(topUrgent([warnA, warnB])).toBe(warnA);
    expect(topUrgent([info])).toBe(info);
  });

  it('lastDeployAt reads the newest deploy-buttons run start', () => {
    expect(DEPLOY_ACTION).toBe('deploy-buttons');
    expect(lastDeployAt([])).toBeNull();
    expect(lastDeployAt([job({ kind: 'briefing', at: 100 })])).toBeNull();
    const runs = [
      job({ id: 'old', kind: DEPLOY_ACTION, at: 100, done: true }),
      job({ id: 'new', name: DEPLOY_ACTION, created_at: 200, done: true }),
      job({ id: 'other', kind: 'briefing', at: 999, done: true }),
    ];
    expect(lastDeployAt(runs)).toBe(200);
  });

  it('lastDeployAt skips deploy runs without a timestamp', () => {
    expect(lastDeployAt([job({ kind: DEPLOY_ACTION, done: true })])).toBeNull();
    expect(lastDeployAt([
      job({ id: 'a', kind: DEPLOY_ACTION }),
      job({ id: 'b', kind: DEPLOY_ACTION, at: 50 }),
    ])).toBe(50);
  });

  it('heroCountsText joins live counts with singulars and deploy age', () => {
    const twoHoursAgo = Math.floor(Date.now() / 1000) - 2 * 3600;
    expect(heroCountsText({ attentionOpen: 2, runsActive: true, prCount: 3, deployAt: twoHoursAgo }))
      .toBe('2 needing attention · 1 active run · 3 PRs open · deploy 2h ago');
    expect(heroCountsText({ attentionOpen: 0, runsActive: false, prCount: 1, deployAt: null }))
      .toBe('0 needing attention · no active runs · 1 PR open · no recent deploys');
  });

  it('heroCountsText omits the PR segment when the count is unknown', () => {
    expect(heroCountsText({ attentionOpen: 1, runsActive: false, prCount: null, deployAt: null }))
      .toBe('1 needing attention · no active runs · no recent deploys');
  });

  it('NowHero renders one strip with status, counts, and the top action', () => {
    const top = item({ id: 'urgent', severity: 'warn', title: 'Failing workflow' });
    const html = renderToStaticMarkup(
      <NowHero
        health={health()}
        feed={feed({ attention: [top], runs: { active: { id: 'r1', action: 'briefing', started: 1 }, recent: [] } })}
        prCount={2}
        deployAt={null}
        onDone={() => {}}
        onError={() => {}}
        onSection={() => {}}
        onTab={() => {}}
      />,
    );
    expect(html).toContain('data-testid="now-hero"');
    expect(html).toContain('Status summary');
    expect(html).toContain('data-testid="now-hero-dot"');
    expect(html).toContain('dot-warn');
    expect(html).toContain('Needs attention');
    expect(html).toContain('1 needing attention · 1 active run · 2 PRs open · no recent deploys');
    expect(html).toContain('data-testid="now-hero-urgent"');
    expect(html).toContain('Top: Failing workflow');
    expect(html).toContain('View jobs');
  });

  it('NowHero reads all clear with an empty queue and healthy stack', () => {
    const html = renderToStaticMarkup(
      <NowHero
        health={health()}
        feed={feed()}
        prCount={0}
        deployAt={null}
        onDone={() => {}}
        onError={() => {}}
        onSection={() => {}}
        onTab={() => {}}
      />,
    );
    expect(html).toContain('dot-ok');
    expect(html).toContain('All clear');
    expect(html).toContain('Nothing urgent.');
  });

  it('NowHero escalates a sick stack and a missing snapshot', () => {
    const sick = renderToStaticMarkup(
      <NowHero
        health={health({ containers_error: 'boom' })}
        feed={feed()}
        prCount={null}
        deployAt={null}
        onDone={() => {}}
        onError={() => {}}
        onSection={() => {}}
        onTab={() => {}}
      />,
    );
    expect(sick).toContain('dot-bad');
    expect(sick).toContain('Action needed');
    expect(sick).not.toContain('PRs open');
    const loading = renderToStaticMarkup(
      <NowHero
        health={null}
        feed={feed()}
        prCount={null}
        deployAt={null}
        onDone={() => {}}
        onError={() => {}}
        onSection={() => {}}
        onTab={() => {}}
      />,
    );
    expect(loading).toContain('dot-idle');
    expect(loading).toContain('Loading status…');
  });
});

describe('guided empty state (issue #809)', () => {
  it('explains what lands here with a run-a-check action', () => {
    const html = renderToStaticMarkup(<NowEmptyState onRunCheck={() => {}} />);
    expect(html).toContain('data-testid="now-empty"');
    expect(html).toContain('Nothing needs you right now');
    expect(html).toContain('Failing checks, approvals, and incidents will land here.');
    expect(html).toContain('data-testid="now-empty-jobs"');
    expect(html).toContain('>Run a check<');
  });
});

function attentionItem(over: Partial<AttentionItem> = {}): AttentionItem {
  return {
    id: 'a1',
    kind: 'job',
    title: 'Failed job',
    detail: 'd',
    repo: 'r',
    severity: 'info',
    action: { label: 'Open', method: 'GET', href: '/api/jobs', destructive: false, intent: 'open' },
    created_at: 1700000000,
    ...over,
  };
}

describe('severity filter chips (issue #813)', () => {
  it('normalizes long-form and unknown severities', () => {
    expect(sevKey('crit')).toBe('crit');
    expect(sevKey('critical')).toBe('crit');
    expect(sevKey('warn')).toBe('warn');
    expect(sevKey('warning')).toBe('warn');
    expect(sevKey('info')).toBe('info');
    expect(sevKey('something-new')).toBe('info');
  });

  it('counts per severity across the feed', () => {
    const items = [
      attentionItem({ id: 'c', severity: 'crit' }),
      attentionItem({ id: 'w', severity: 'warn' }),
      attentionItem({ id: 'i1', severity: 'info' }),
      attentionItem({ id: 'i2', severity: 'info' }),
    ];
    expect(severityCounts(items)).toEqual({ crit: 1, warn: 1, info: 2 });
    expect(severityCounts([])).toEqual({ crit: 0, warn: 0, info: 0 });
  });

  it('filters instantly with all-off hiding everything', () => {
    const items = [
      attentionItem({ id: 'c', severity: 'crit' }),
      attentionItem({ id: 'i', severity: 'info' }),
    ];
    expect(filterBySeverity(items, { crit: true, warn: true, info: true })).toHaveLength(2);
    expect(filterBySeverity(items, { crit: true, warn: true, info: false }).map((i) => i.id)).toEqual(['c']);
    expect(filterBySeverity(items, { crit: false, warn: false, info: false })).toEqual([]);
  });

  it('renders one pressed-state chip per severity with counts', () => {
    const html = renderToStaticMarkup(
      <SeverityChips
        counts={{ crit: 1, warn: 2, info: 3 }}
        visible={{ crit: true, warn: false, info: true }}
        onToggle={() => {}}
      />,
    );
    expect(html).toContain('data-testid="now-sev-filters"');
    for (const s of SEVERITY_FILTERS) expect(html).toContain(`data-testid="now-sev-${s}"`);
    expect(html).toContain('(1)');
    expect(html).toContain('(2)');
    expect(html).toContain('(3)');
    expect(html).toContain('aria-pressed="true"');
    expect(html).toContain('aria-pressed="false"');
    expect(html).toContain('sev-crit');
    expect(html).toContain('sev-warn');
    expect(html).toContain('sev-info');
  });
});

describe('snooze-all-info targets (issue #815)', () => {
  it('scopes to visible info rows without stubs', () => {
    const items = [
      attentionItem({ id: 'c', severity: 'crit' }),
      attentionItem({ id: 'i1', severity: 'info' }),
      attentionItem({ id: 'i2', severity: 'info' }),
    ];
    expect(snoozeAllInfoTargets(items, {}).map((i) => i.id)).toEqual(['i1', 'i2']);
    expect(
      snoozeAllInfoTargets(items, { i1: { until: 9, title: 't' } }).map((i) => i.id),
    ).toEqual(['i2']);
    expect(snoozeAllInfoTargets([], {})).toEqual([]);
  });
});
