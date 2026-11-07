import { describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import NowView, {
  ActionControl,
  AskAgentControl,
  askAgentPrompt,
  askAgentSeed,
  AttentionBulkBar,
  AttentionGroupRow,
  AttentionRow,
  AttentionSelectAll,
  attentionGroupKey,
  attentionTargetName,
  cardDisplayTitle,
  DEPLOY_ACTION,
  fireHeroSegment,
  groupAttentionItems,
  groupSeenText,
  heroCountsText,
  heroLabel,
  heroSegments,
  heroStatus,
  lastDeployAt,
  mergeServerSnoozed,
  NowEmptyState,
  NowHero,
  partitionAttention,
  reconcileSnoozed,
  repoContextText,
  RepoTag,
  retryBlock,
  retryBlocked,
  SourceDestination,
  resolveAttentionTarget,
  showRepoTag,
  SnoozedStub,
  SNOOZE_PRESETS,
  SnoozeButtons,
  SNOOZE_BUSY_NOTICE,
  snoozeBusyNotice,
  snoozedRows,
  snoozePresetLabel,
  ReportDialog,
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

describe('snooze busy guard (issue #1010)', () => {
  it('stays quiet when idle', () => {
    expect(snoozeBusyNotice(null)).toBeNull();
  });

  it('surfaces a visible notice while a snooze is in flight', () => {
    expect(snoozeBusyNotice('job-j1')).toBe(SNOOZE_BUSY_NOTICE);
    expect(snoozeBusyNotice('bulk')).toBe(SNOOZE_BUSY_NOTICE);
    expect(SNOOZE_BUSY_NOTICE.length).toBeGreaterThan(0);
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

describe('mergeServerSnoozed (issue #1030)', () => {
  const NOW = 1000;

  it('hydrates stubs for server ids the session never snoozed', () => {
    const merged = mergeServerSnoozed(
      {},
      [{ id: 'a', title: 'Failed job', until: 2000, repo: 'remote-server' }],
      [],
      NOW,
    );
    expect(merged).toEqual({ a: { until: 2000, title: 'Failed job' } });
    expect(snoozedRows(merged, [])).toEqual([
      ['a', { until: 2000, title: 'Failed job' }],
    ]);
  });

  it('falls back to the id when the server title is empty', () => {
    const merged = mergeServerSnoozed({}, [{ id: 'a', title: '', until: 2000, repo: 'remote-server' }], [], NOW);
    expect(merged.a.title).toBe('a');
  });

  it('skips expired rows and ids back in the live list', () => {
    const rows = [
      { id: 'old', title: 't', until: 500, repo: 'remote-server' },
      { id: 'back', title: 't', until: 2000, repo: 'remote-server' },
    ];
    expect(mergeServerSnoozed({}, rows, ['back'], NOW)).toEqual({});
  });

  it('lets newer local optimism win over a stale server row', () => {
    const merged = mergeServerSnoozed(
      { a: { until: 3000, title: 'local' } },
      [{ id: 'a', title: 'server', until: 2000, repo: 'remote-server' }],
      [],
      NOW,
    );
    expect(merged.a).toEqual({ until: 3000, title: 'local' });
  });

  it('lets a newer server row replace local optimism', () => {
    const merged = mergeServerSnoozed(
      { a: { until: 2000, title: 'local' } },
      [{ id: 'a', title: 'server', until: 3000, repo: 'remote-server' }],
      [],
      NOW,
    );
    expect(merged.a).toEqual({ until: 3000, title: 'server' });
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

describe('partitionAttention (issue #1109.6)', () => {
  const item = (id: string): AttentionItem => ({
    id,
    kind: 'workflow',
    title: 'console-e2e fails',
    detail: '',
    repo: 'remote-server',
    severity: 'warn',
    action: { label: 'x', method: 'GET', href: 'api/x', destructive: false, intent: 'x' },
    created_at: 1,
  });
  it('passes everything through when nothing is snoozed', () => {
    const items = [item('a'), item('b')];
    expect(partitionAttention(items, {})).toEqual({ active: items, snoozed: [] });
  });
  it('moves snoozed members out of the actionable list', () => {
    const items = [item('a'), item('b'), item('c')];
    const snoozed = { b: { until: 9, title: 'B' }, c: { until: 9, title: 'C' } };
    const { active, snoozed: moved } = partitionAttention(items, snoozed);
    expect(active.map((i) => i.id)).toEqual(['a']);
    expect(moved.map((i) => i.id)).toEqual(['b', 'c']);
  });
  it('keeps feed order within each side', () => {
    const items = [item('s1'), item('a1'), item('s2')];
    const snoozed = { s1: { until: 1, title: 'S1' }, s2: { until: 1, title: 'S2' } };
    const { active, snoozed: moved } = partitionAttention(items, snoozed);
    expect(active.map((i) => i.id)).toEqual(['a1']);
    expect(moved.map((i) => i.id)).toEqual(['s1', 's2']);
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

  it('keeps non-destructive POST actions as primary buttons (issue #896)', () => {
    const html = renderToStaticMarkup(
      <ActionControl item={item({ destructive: false })} {...handlers} />,
    );
    expect(html).not.toContain('tap-fire');
    expect(html).not.toContain('hold-disarmed');
    expect(html).toContain('class="btn-primary"');
  });

  it('keeps GET actions as primary buttons (issue #896)', () => {
    const html = renderToStaticMarkup(
      <ActionControl item={item({ method: 'GET', destructive: true })} {...handlers} />,
    );
    expect(html).not.toContain('tap-fire');
    expect(html).not.toContain('hold-disarmed');
    expect(html).toContain('class="btn-primary"');
  });
});

describe('source destination links (issue #896)', () => {
  function item(href: string): AttentionItem {
    return {
      id: 'a1',
      kind: 'failed-job',
      title: 'Failed job',
      detail: 'd',
      repo: 'remote-server',
      severity: 'warn',
      action: { label: 'Open', method: 'GET', href, destructive: false, intent: '' },
      created_at: 1,
    };
  }
  const noop = () => {};

  it('renders in-app targets as buttons', () => {
    const html = renderToStaticMarkup(
      <SourceDestination item={item('api/jobs')} onSection={noop} onTab={noop} />,
    );
    expect(html).toContain('data-testid="now-source-open-a1"');
    expect(html).toContain('class="btn-link"');
    expect(html).toContain('>Jobs</button>');
  });

  it('renders nothing for fetch-only actions (issue #930)', () => {
    const html = renderToStaticMarkup(
      <SourceDestination item={item('api/unknown')} onSection={noop} onTab={noop} />,
    );
    expect(html).toBe('');
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
    // All clear needs a truly empty queue (issue #930): even an
    // info-only item keeps the banner amber.
    expect(heroStatus(health(), [item({ severity: 'info' })])).toBe('warn');
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
        onAttention={() => {}}
        onHistoryDeploys={() => {}}
      />,
    );
    expect(html).toContain('data-testid="now-hero"');
    expect(html).toContain('Status summary');
    expect(html).toContain('data-testid="now-hero-dot"');
    expect(html).toContain('dot-warn');
    expect(html).toContain('1 needing attention');
    expect(html).toContain('data-testid="now-hero-urgent"');
    expect(html).toContain('Top: Failing workflow');
    expect(html).toContain('View jobs');
  });

  it('NowHero renders every count as a deep-link button (issue #930)', () => {
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
        onAttention={() => {}}
        onHistoryDeploys={() => {}}
      />,
    );
    expect(html).toContain('data-testid="now-hero-seg-attention"');
    expect(html).toContain('>1 needing attention</button>');
    expect(html).toContain('data-testid="now-hero-seg-runs"');
    expect(html).toContain('>1 active run</button>');
    expect(html).toContain('data-testid="now-hero-seg-prs"');
    expect(html).toContain('>2 PRs open</button>');
    expect(html).toContain('data-testid="now-hero-seg-deploys"');
    expect(html).toContain('>no recent deploys</button>');
  });

  it('NowHero hides the duplicate count from screen readers (issue #1068)', () => {
    const top = item({ id: 'urgent', severity: 'warn', title: 'Failing workflow' });
    const html = renderToStaticMarkup(
      <NowHero
        health={health()}
        feed={feed({ attention: [top], runs: { active: null, recent: [] } })}
        prCount={null}
        deployAt={null}
        onDone={() => {}}
        onError={() => {}}
        onSection={() => {}}
        onTab={() => {}}
        onAttention={() => {}}
        onHistoryDeploys={() => {}}
      />,
    );
    // The attention segment button already announces the count.
    expect(html).toContain('data-testid="now-hero-status" aria-hidden="true"');
  });

  it('NowHero keeps non-count labels exposed to screen readers (issue #1068)', () => {
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
        onAttention={() => {}}
        onHistoryDeploys={() => {}}
      />,
    );
    expect(html).toContain('All clear');
    expect(html).not.toContain('now-hero-status" aria-hidden');
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
        onAttention={() => {}}
        onHistoryDeploys={() => {}}
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
        onAttention={() => {}}
        onHistoryDeploys={() => {}}
      />,
    );
    expect(sick).toContain('dot-bad');
    expect(sick).toContain('Action needed');
    expect(sick).not.toContain('now-hero-seg-prs');
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
        onAttention={() => {}}
        onHistoryDeploys={() => {}}
      />,
    );
    expect(loading).toContain('dot-idle');
    expect(loading).toContain('Loading status…');
  });
});

describe('triage banner dynamics (issue #930)', () => {
  it('heroLabel names the queue depth while items are queued', () => {
    expect(heroLabel('loading', 0)).toBe('Loading status…');
    expect(heroLabel('ok', 0)).toBe('All clear');
    expect(heroLabel('warn', 1)).toBe('1 needing attention');
    expect(heroLabel('warn', 3)).toBe('3 needing attention');
    expect(heroLabel('crit', 2)).toBe('2 needing attention');
  });

  it('heroLabel names the stack state on an empty queue', () => {
    expect(heroLabel('warn', 0)).toBe('Needs attention');
    expect(heroLabel('crit', 0)).toBe('Action needed');
  });

  it('heroSegments maps every count to its deep-link target', () => {
    const segments = heroSegments({ attentionOpen: 1, runsActive: true, prCount: 2, deployAt: null });
    expect(segments.map((s) => [s.key, s.text, s.target])).toEqual([
      ['attention', '1 needing attention', 'attention'],
      ['runs', '1 active run', 'jobs'],
      ['prs', '2 PRs open', 'review'],
      ['deploys', 'no recent deploys', 'history'],
    ]);
  });

  it('heroSegments omits the PR segment when the count is unknown', () => {
    const segments = heroSegments({ attentionOpen: 0, runsActive: false, prCount: null, deployAt: null });
    expect(segments.map((s) => s.key)).toEqual(['attention', 'runs', 'deploys']);
    expect(segments[0].text).toBe('0 needing attention');
  });

  it('fireHeroSegment dispatches each target to its callback', () => {
    const cbs = { onAttention: vi.fn(), onSection: vi.fn(), onHistoryDeploys: vi.fn() };
    fireHeroSegment('attention', cbs);
    expect(cbs.onAttention).toHaveBeenCalledTimes(1);
    fireHeroSegment('review', cbs);
    expect(cbs.onSection).toHaveBeenCalledWith('review');
    fireHeroSegment('jobs', cbs);
    expect(cbs.onSection).toHaveBeenCalledWith('jobs');
    fireHeroSegment('history', cbs);
    expect(cbs.onHistoryDeploys).toHaveBeenCalledTimes(1);
    expect(cbs.onAttention).toHaveBeenCalledTimes(1);
  });

  it('repoContextText names the branch and flags dirty worktrees', () => {
    expect(repoContextText('remote-server', null)).toBe('repo: remote-server');
    expect(repoContextText('remote-server', undefined)).toBe('repo: remote-server');
    expect(
      repoContextText('remote-server', { name: 'remote-server', full: 'arii/remote-server', branch: 'main', clean: true }),
    ).toBe('repo: remote-server (main)');
    expect(
      repoContextText('boomtick', { name: 'boomtick', full: 'arii/boomtick', branch: 'feat', clean: false }),
    ).toBe('repo: boomtick (feat) · dirty worktree');
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

describe('attention grouping (issue #839)', () => {
  function item(overrides: Partial<AttentionItem> = {}): AttentionItem {
    return {
      id: 'a1',
      kind: 'failed-job',
      title: 'check-notifications failed',
      detail: 'd',
      repo: 'remote-server',
      severity: 'warn',
      action: { label: 'Open', method: 'GET', href: 'api/jobs', destructive: false, intent: '' },
      created_at: 1000,
      ...overrides,
    };
  }

  it('keys groups on kind + title', () => {
    expect(attentionGroupKey(item())).toBe('failed-job‖check-notifications failed');
  });

  it('derives a card title from a bracket tag when the title is blank', () => {
    expect(cardDisplayTitle(item({ title: '', detail: '[CI] 2 failing: build, e2e' }))).toBe('CI');
    expect(cardDisplayTitle(item({ title: 'Attention', detail: '[E2E] smoke red' }))).toBe('E2E');
    expect(cardDisplayTitle(item({ title: 'Failed job', detail: '[CI] x' }))).toBe('Failed job');
    expect(cardDisplayTitle(item({ title: '', detail: 'plain detail' }))).toBe('');
  });

  it('folds repeats into one group in feed order with worst severity', () => {
    const groups = groupAttentionItems([
      item({ id: 'a1', severity: 'warn', created_at: 1000 }),
      item({ id: 'other', kind: 'stale', title: 'Other', created_at: 2000 }),
      item({ id: 'a2', severity: 'crit', created_at: 3000 }),
    ]);
    expect(groups.length).toBe(2);
    expect(groups[0].items.map((i) => i.id)).toEqual(['a1', 'a2']);
    expect(groups[0].severity).toBe('crit');
    expect(groups[0].firstSeen).toBe(1000);
    expect(groups[0].lastSeen).toBe(3000);
    expect(groups[1].items.map((i) => i.id)).toEqual(['other']);
  });

  it('reads first/last seen, collapsing single sightings', () => {
    const [g] = groupAttentionItems([
      item({ id: 'a1', created_at: 1000 }),
      item({ id: 'a2', created_at: 3000 }),
    ]);
    expect(groupSeenText(g)).toContain('first ');
    expect(groupSeenText(g)).toContain('last ');
    const [solo] = groupAttentionItems([item({ id: 'a1', created_at: 1000 })]);
    expect(groupSeenText(solo)).not.toContain('first ');
    const [unknown] = groupAttentionItems([item({ id: 'a1', created_at: 0 })]);
    expect(groupSeenText(unknown)).toBe('unknown time');
  });

  it('names the action destination for the source line', () => {
    expect(attentionTargetName('api/jobs')).toBe('Jobs');
    expect(attentionTargetName('/api/prs?repo=x')).toBe('Review');
    expect(attentionTargetName('api/workflow-health')).toBe('History');
    expect(attentionTargetName('api/notifications/status')).toBe('Briefing');
    expect(attentionTargetName('api/agents/budget')).toBe('Chat');
    expect(attentionTargetName('api/unknown')).toBe('action');
  });

  it('AttentionRow surfaces snooze and dismiss inline, 8h in overflow (issue #935)', () => {
    const noop = () => {};
    const html = renderToStaticMarkup(
      <AttentionRow
        item={item({ id: 'job-j1' })}
        viewRepo="remote-server"
        rollup={false}
        selected={false}
        busy={false}
        onToggleSelect={noop}
        onDone={noop}
        onError={noop}
        onSection={noop}
        onTab={noop}
        onAskAgent={noop}
        onSnooze={noop}
      />,
    );
    expect(html).toContain('data-testid="now-item-job-j1"');
    expect(html).toContain('data-testid="now-select-job-j1"');
    expect(html).toContain('class="attention-head"');
    expect(html).toContain('data-testid="now-source-job-j1"');
    expect(html).toContain('repo: remote-server');
    expect(html).toContain('· opens');
    expect(html).toContain('data-testid="now-source-open-job-j1"');
    expect(html).toContain('>Jobs</button>');
    expect(html).toContain('data-testid="ask-agent-job-j1"');
    expect(html).toContain('class="btn-secondary"');
    expect(html).toContain('data-testid="now-more-job-j1"');
    expect(html).toContain('data-testid="snooze-job-j1-60"');
    expect(html).toContain('data-testid="snooze-job-j1-480"');
    expect(html).toContain('data-testid="dismiss-job-j1"');
    // Snooze 1h and Dismiss render before the More overflow.
    expect(html.indexOf('snooze-job-j1-60')).toBeLessThan(html.indexOf('now-more-job-j1'));
    expect(html.indexOf('dismiss-job-j1')).toBeLessThan(html.indexOf('now-more-job-j1'));
    // The overflow never carries Review/Jobs nav links.
    const overflow = html.slice(html.indexOf('now-more-job-j1'));
    expect(overflow).not.toContain('>Jobs<');
    expect(overflow).not.toContain('>Review<');
  });

  it('AttentionRow shows repo context without an opens run for fetch-only actions (issue #930)', () => {
    const noop = () => {};
    const html = renderToStaticMarkup(
      <AttentionRow
        item={item({
          id: 'fetch-f1',
          action: { label: 'Retry pull', method: 'POST', href: 'api/unknown', destructive: false, intent: 'retry' },
        })}
        viewRepo="remote-server"
        rollup={false}
        repoInfo={{ name: 'remote-server', full: 'arii/remote-server', branch: 'main', clean: false }}
        selected={false}
        busy={false}
        onToggleSelect={noop}
        onDone={noop}
        onError={noop}
        onSection={noop}
        onTab={noop}
        onAskAgent={noop}
        onSnooze={noop}
      />,
    );
    expect(html).toContain('repo: remote-server (main) · dirty worktree');
    expect(html).not.toContain('opens');
    expect(html).not.toContain('now-source-open-fetch-f1');
  });

  it('AttentionGroupRow shows the count, worst severity, and seen range', () => {
    const [g] = groupAttentionItems([
      item({ id: 'a1', severity: 'warn', created_at: 1000 }),
      item({ id: 'a2', severity: 'crit', created_at: 3000 }),
    ]);
    const html = renderToStaticMarkup(
      <AttentionGroupRow group={g} index={0}>
        <li>member</li>
      </AttentionGroupRow>,
    );
    expect(html).toContain('data-testid="now-group-0"');
    expect(html).toContain('check-notifications failed ×2');
    expect(html).toContain('sev-crit');
    expect(html).toContain('first ');
  });

  it('AttentionBulkBar names the count with one button per preset', () => {
    const html = renderToStaticMarkup(
      <AttentionBulkBar count={3} busy={false} onSnooze={() => {}} onClear={() => {}} />,
    );
    expect(html).toContain('data-testid="now-bulk-bar"');
    expect(html).toContain('3 selected');
    expect(html).toContain('data-testid="now-bulk-snooze-60"');
    expect(html).toContain('data-testid="now-bulk-snooze-480"');
    expect(html).toContain('data-testid="now-bulk-clear"');
  });
});

describe('now loading skeleton (issue #897)', () => {
  it('reads as Now while the first fetch lands', () => {
    const html = renderToStaticMarkup(
      <NowView
        repo="r"
        health={null}
        reportContext="c"
        onAskAgent={() => {}}
        onOpenTab={() => {}}
        onSectionChange={() => {}}
        onOpenStats={() => {}}
      />,
    );
    expect(html).toContain('data-testid="now-loading"');
    expect(html).toContain('>Now<');
    expect(html).toContain('Loading attention, pull requests, and jobs…');
  });
});

describe('triage card CTAs (issue #935)', () => {
  const noop = () => {};
  const rowHandlers = {
    onToggleSelect: noop,
    onDone: noop,
    onError: noop,
    onSection: noop,
    onTab: noop,
    onAskAgent: noop,
    onSnooze: noop,
  };

  function skip(overrides: Partial<AttentionItem> = {}): AttentionItem {
    return {
      id: 'webhook-skip-1',
      kind: 'skipped-webhook',
      title: 'Webhook pull skipped — checkout lagging',
      detail: 'skipped: tree not ff-able — behind by 264 (local changes)',
      repo: 'remote-server',
      severity: 'info',
      action: {
        label: 'Retry pull',
        method: 'POST',
        href: 'api/webhook/retry?repo=remote-server',
        destructive: false,
        intent: '',
      },
      created_at: 1,
      ...overrides,
    };
  }

  function renderRow(itemToRender: AttentionItem, extra: Record<string, unknown> = {}): string {
    return renderToStaticMarkup(
      <AttentionRow
        item={itemToRender}
        viewRepo="remote-server"
        rollup={false}
        selected={false}
        busy={false}
        {...rowHandlers}
        {...(extra as object)}
      />,
    );
  }

  it('classifies blocked retries from the skip detail', () => {
    expect(retryBlock(skip())).toBe('diverged');
    expect(retryBlock(skip({ detail: 'skipped: tree dirty — behind by 3' }))).toBe('dirty');
    expect(retryBlock(skip({ detail: 'skipped: tree not ff-able' }))).toBe('diverged');
    expect(retryBlock(skip({ detail: 'skipped: fetch did not complete' }))).toBeNull();
    expect(retryBlock(skip({ kind: 'failed-job' }))).toBeNull();
  });

  it('blocks retry while the checkout is dirty even without a reason in the detail', () => {
    const vague = skip({ detail: 'skipped: pull skipped' });
    expect(retryBlocked(vague, null)).toBeNull();
    expect(
      retryBlocked(vague, {
        name: 'remote-server',
        full: 'arii/remote-server',
        branch: 'main',
        clean: false,
      }),
    ).toBe('dirty');
    expect(
      retryBlocked(vague, {
        name: 'remote-server',
        full: 'arii/remote-server',
        branch: 'main',
        clean: true,
      }),
    ).toBeNull();
  });

  it('demotes Retry pull and promotes Ask agent on a diverged card', () => {
    const html = renderRow(skip());
    expect(html).toContain('Ask agent to inspect &amp; rebase');
    expect(html).toContain('data-testid="now-retry-webhook-skip-1"');
    expect(html).toContain('disabled');
    expect(html).toContain('Retry will fail until the local commits are rebased');
    expect(html).not.toContain('class="btn-primary">Retry pull');
    expect(html).not.toContain('class="btn-primary">');
  });

  it('demotes Retry pull on a dirty checkout flagged by repo info', () => {
    const html = renderRow(skip({ detail: 'skipped: pull skipped' }), {
      repoInfo: {
        name: 'remote-server',
        full: 'arii/remote-server',
        branch: 'main',
        clean: false,
      },
    });
    expect(html).toContain('data-testid="now-retry-webhook-skip-1"');
    expect(html).toContain('Retry will fail until the local changes are stashed or committed');
    expect(html).not.toContain('class="btn-primary">Retry pull');
  });

  it('keeps Retry pull primary when the skip can succeed', () => {
    const html = renderRow(skip({ detail: 'skipped: a racing git op held the ref lock' }));
    expect(html).toContain('>Retry pull</button>');
    expect(html).toContain('class="btn-primary"');
    expect(html).not.toContain('now-retry-webhook-skip-1');
    expect(html).not.toContain('Ask agent to inspect');
  });

  it('builds exact ask-agent prompts per mode from the row seed', () => {
    const itemToSend = skip();
    expect(askAgentPrompt(itemToSend, 'diagnose')).toBe(
      `Read-only diagnosis — do not modify any files. Inspect and explain: ${askAgentSeed(itemToSend)}`,
    );
    expect(askAgentPrompt(itemToSend, 'fix')).toBe(
      `Auto-fix in a worktree — inspect, fix, and verify: ${askAgentSeed(itemToSend)}`,
    );
  });

  it('AskAgentControl previews the prompt and context with the mode split', () => {
    const html = renderToStaticMarkup(<AskAgentControl item={skip()} onAskAgent={noop} />);
    expect(html).toContain('data-testid="ask-agent-webhook-skip-1"');
    expect(html).toContain('data-testid="ask-agent-preview-webhook-skip-1"');
    expect(html).toContain('data-testid="ask-agent-context-webhook-skip-1"');
    expect(html).toContain(askAgentSeed(skip()));
    expect(html).toContain('data-testid="ask-agent-prompt-diagnose-webhook-skip-1"');
    expect(html).toContain('Read-only diagnosis — do not modify any files.');
    expect(html).toContain('data-testid="ask-agent-prompt-fix-webhook-skip-1"');
    expect(html).toContain('Auto-fix in a worktree — inspect, fix, and verify:');
    expect(html).toContain('data-testid="ask-agent-diagnose-webhook-skip-1"');
    expect(html).toContain('>Read-Only Diagnosis</button>');
    expect(html).toContain('data-testid="ask-agent-fix-webhook-skip-1"');
    expect(html).toContain('>Auto-Fix in Worktree</button>');
  });

  it('hides the card checkbox when the list opts out of bulk selection', () => {
    expect(renderRow(skip(), { showSelect: false })).not.toContain('now-select-webhook-skip-1');
    expect(renderRow(skip())).toContain('data-testid="now-select-webhook-skip-1"');
    expect(renderRow(skip())).toContain('class="attention-head"');
  });

  it('AttentionSelectAll reflects the selection state', () => {
    const none = renderToStaticMarkup(
      <AttentionSelectAll total={3} selectedCount={0} onToggle={noop} />,
    );
    expect(none).toContain('data-testid="now-select-all"');
    expect(none).toContain('Select all');
    expect(none).not.toContain('checked');
    const all = renderToStaticMarkup(
      <AttentionSelectAll total={3} selectedCount={3} onToggle={noop} />,
    );
    expect(all).toContain('checked');
  });

  function heroHealth(): SystemHealthResponse {
    return {
      disk: {},
      memory: {},
      containers: [{ name: 'c', service: 'c', state: 'running', status: 'up', health: 'healthy' }],
      containers_error: null,
    };
  }

  function heroFeed(attention: AttentionItem[]): InboxFeed {
    return {
      repo: 'remote-server',
      updated_at: 1,
      attention,
      merge_ready: { count: 0, top: [] },
      runs: { active: null, recent: [] },
    };
  }

  it('hides the hero Retry pull when the same card renders first below', () => {
    const html = renderToStaticMarkup(
      <NowHero
        health={heroHealth()}
        feed={heroFeed([skip()])}
        prCount={0}
        deployAt={null}
        onDone={noop}
        onError={noop}
        onSection={noop}
        onTab={noop}
        onAttention={noop}
        onHistoryDeploys={noop}
      />,
    );
    expect(html).toContain('Top: Webhook pull skipped — checkout lagging');
    expect(html).not.toContain('>Retry pull<');
    expect(html).not.toContain('now-hero-retry');
  });

  it('demotes the hero Retry pull for a blocked card that is not first', () => {
    const first: AttentionItem = {
      id: 'other-1',
      kind: 'failed-job',
      title: 'Failed job: nightly',
      detail: 'd',
      repo: 'remote-server',
      severity: 'info',
      action: { label: 'View jobs', method: 'GET', href: 'api/jobs', destructive: false, intent: '' },
      created_at: 1,
    };
    const blocked = skip({ severity: 'crit' as AttentionItem['severity'] });
    const html = renderToStaticMarkup(
      <NowHero
        health={heroHealth()}
        feed={heroFeed([first, blocked])}
        prCount={0}
        deployAt={null}
        onDone={noop}
        onError={noop}
        onSection={noop}
        onTab={noop}
        onAttention={noop}
        onHistoryDeploys={noop}
      />,
    );
    expect(html).toContain('data-testid="now-hero-retry"');
    expect(html).toContain('disabled');
    expect(html).not.toContain('class="btn-primary">Retry pull');
  });
});

describe('report-a-bug dialog compactness (issue #1027)', () => {
  function renderDialog(): string {
    return renderToStaticMarkup(
      <ReportDialog
        initialBody=""
        availableLabels={['bug', 'enhancement', 'question']}
        onSubmit={() => {}}
        onCancel={() => {}}
      />,
    );
  }

  it('puts title and details before the label picker', () => {
    const html = renderDialog();
    expect(html).toContain('data-testid="report-close"');
    expect(html.indexOf('data-testid="report-title"')).toBeLessThan(
      html.indexOf('data-testid="report-labels"'),
    );
    expect(html.indexOf('data-testid="report-body"')).toBeLessThan(
      html.indexOf('data-testid="report-labels"'),
    );
  });

  it('collapses labels into chips with per-label testids intact', () => {
    const html = renderDialog();
    expect(html).toContain('<details');
    expect(html).toContain('Labels (optional)');
    expect(html).toContain('data-testid="report-label-bug"');
    expect(html).toContain('data-testid="report-label-enhancement"');
    expect(html).toContain('label-chip');
  });
});
