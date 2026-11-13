import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { dangerEntryFor } from './DangerZone';
import {
  CompareRunsSection,
  createReentryGuard,
  HighlightedLine,
  JobListItem,
  JobsListPanel,
  PlanCardPanel,
  RunComparePanel,
  RunOutputDrawer,
  RunProgressPanel,
  SideBySideDiffView,
  TriageFanoutSection,
  UnifiedDiffView,
  UxAuditGroups,
  UxAuditSection,
  excerptJobNote,
} from './JobsView';
import type { JobRecord } from '../apiTypes';

function mkJob(over: Partial<JobRecord> = {}): JobRecord {
  return { id: 'abc123def456', kind: 'logs', name: 'logs', repo: 'remote-server', done: true, ...over };
}

describe('re-entry guard (issue #1070)', () => {
  it('absorbs overlapping runs while one is in flight', async () => {
    const run = createReentryGuard();
    let calls = 0;
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const first = run(async () => {
      calls += 1;
      await gate;
    });
    await run(async () => {
      calls += 1;
    });
    expect(calls).toBe(1);
    release();
    await first;
    await run(async () => {
      calls += 1;
    });
    expect(calls).toBe(2);
  });

  it('lets a different action proceed while one is in flight (issue #1090)', async () => {
    const run = createReentryGuard();
    let calls: string[] = [];
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const first = run(async () => {
      calls.push('poll');
      await gate;
    }, 'load jobs');
    await run(async () => {
      calls.push('launch');
    }, 'td doctor');
    expect(calls).toEqual(['poll', 'launch']);
    release();
    await first;
  });

  it('releases the guard when the run throws', async () => {
    const run = createReentryGuard();
    let calls = 0;
    await expect(
      run(async () => {
        calls += 1;
        throw new Error('boom');
      }),
    ).rejects.toThrow('boom');
    await run(async () => {
      calls += 1;
    });
    expect(calls).toBe(2);
  });
});

describe('row note excerpt (issue #1013)', () => {
  it('caps long notes with an ellipsis', () => {
    expect(excerptJobNote('short note')).toBe('short note');
    expect(excerptJobNote('  spaced   out  ')).toBe('spaced out');
    const long = `failed: ${'x'.repeat(300)}`;
    const out = excerptJobNote(long);
    expect(out.length).toBeLessThanOrEqual(160);
    expect(out.endsWith('…')).toBe(true);
  });

  it('renders the note capped with the full text on hover', () => {
    const long = `failed: ${'y'.repeat(300)}`;
    const html = renderToStaticMarkup(
      <JobListItem
        job={{ id: 'abc123def456', kind: 'logs', name: 'logs', repo: 'remote-server', done: true, error: long }}
        repo="remote-server"
        onSelect={() => {}}
        onRerun={() => {}}
        onCancel={() => {}}
      />,
    );
    expect(html).toContain('class="row-meta job-row-note"');
    expect(html).toContain(`title="${long}"`);
    expect(html).toContain('…</p>');
  });
});

describe('job re-run button (issue #630)', () => {
  it('shows Re-run on finished runs only', () => {
    const done = renderToStaticMarkup(
      <JobListItem job={mkJob()} repo="remote-server" onSelect={() => {}} onRerun={() => {}} onCancel={() => {}} />,
    );
    expect(done).toContain('data-testid="job-rerun"');
    expect(done).toContain('>Re-run<');
    const active = renderToStaticMarkup(
      <JobListItem job={mkJob({ done: false })} repo="remote-server" onSelect={() => {}} onRerun={() => {}} onCancel={() => {}} />,
    );
    expect(active).not.toContain('data-testid="job-rerun"');
  });

  it('asks for confirmation on destructive re-runs, fires directly otherwise', () => {
    const danger = renderToStaticMarkup(
      <JobListItem
        job={mkJob({ kind: 'recreate-code', name: 'recreate-code' })}
        repo="remote-server"
        onSelect={() => {}}
        onRerun={() => {}}
        onCancel={() => {}}
      />,
    );
    expect(danger).toContain('data-testid="job-rerun"');
    expect(danger).toContain('aria-label="Re-run (asks for confirmation)"');
    const safe = renderToStaticMarkup(
      <JobListItem job={mkJob()} repo="remote-server" onSelect={() => {}} onRerun={() => {}} onCancel={() => {}} />,
    );
    expect(safe).not.toContain('asks for confirmation');
  });

  it('resolves destructive confirm text from the danger list', () => {
    expect(dangerEntryFor('recreate-code')).toEqual({ confirm: 'The code editor will restart.' });
    expect(dangerEntryFor('create-plan')).toEqual({});
    expect(dangerEntryFor('logs')).toBeNull();
    // Terminal restart is not offered: sessions cannot survive
    // it and there is no resume path (#444).
    expect(dangerEntryFor('recreate')).toBeNull();
  });
});

describe('job cancel button (issue #589)', () => {
  const noop = () => {};
  it('shows Cancel on the running run only, with no confirmation step', () => {
    const active = renderToStaticMarkup(
      <JobListItem job={mkJob({ done: false })} repo="remote-server" onSelect={noop} onRerun={noop} onCancel={noop} />,
    );
    expect(active).toContain('data-testid="job-cancel"');
    expect(active).toContain('>Cancel<');
    expect(active).not.toContain('asks for confirmation');
    const done = renderToStaticMarkup(
      <JobListItem job={mkJob()} repo="remote-server" onSelect={noop} onRerun={noop} onCancel={noop} />,
    );
    expect(done).not.toContain('data-testid="job-cancel"');
  });
});

describe('output filter (issue #657)', () => {
  it('marks query matches case-insensitively', () => {
    const html = renderToStaticMarkup(<HighlightedLine line="FAIL: assertion boom, failed" query="fail" />);
    expect(html).toContain('<mark>FAIL</mark>');
    expect(html).toContain('<mark>fail</mark>');
    expect(html).toContain('assertion boom');
  });

  it('renders the line as-is on empty query', () => {
    const html = renderToStaticMarkup(<HighlightedLine line="plain line" query="  " />);
    expect(html).toBe('plain line');
    expect(html).not.toContain('<mark>');
  });

  it('shows the filter input with the full line count by default', () => {
    const html = renderToStaticMarkup(
      <RunProgressPanel tail={'a\nb\nc'} runId="abc123def456" running={null} panelRef={{ current: null }} />,
    );
    expect(html).toContain('data-testid="jobs-output-filter"');
    expect(html).toContain('data-testid="jobs-output-count"');
    expect(html).toContain('3 lines');
    expect(html).not.toContain('<mark>');
  });
});

describe('run compare (issue #657)', () => {
  const diff = '--- a/aaa\n+++ b/bbb\n@@ -1,2 +1,2 @@\n same\n-old\n+new';

  it('renders unified diff lines with added/removed coloring', () => {
    const html = renderToStaticMarkup(<UnifiedDiffView diff={diff} />);
    expect(html).toContain('data-testid="run-compare-diff"');
    expect(html).toContain('class="tone-ok"');
    expect(html).toContain('class="tone-bad"');
    expect(html).toContain('class="tone-muted"');
    expect(html).toContain('+new');
    expect(html).toContain('-old');
  });

  it('renders side-by-side rows with a gap for unpaired lines', () => {
    const html = renderToStaticMarkup(<SideBySideDiffView diff={'@@ -1 +1 @@\n-gone\n+added'} aId="aaa" bId="bbb" />);
    expect(html).toContain('data-testid="run-compare-side"');
    expect(html).toContain('<th>aaa</th>');
    expect(html).toContain('<th>bbb</th>');
    expect(html).toContain('>gone<');
    expect(html).toContain('>added<');
  });

  it('says no differences on an empty diff', () => {
    expect(renderToStaticMarkup(<UnifiedDiffView diff="" />)).toContain('No differences.');
    expect(renderToStaticMarkup(<SideBySideDiffView diff="" aId="a" bId="b" />)).toContain('No differences.');
  });

  it('offers two run pickers plus a view toggle', () => {
    const html = renderToStaticMarkup(
      <RunComparePanel
        jobs={[
          mkJob({ id: 'newest111111' }),
          mkJob({ id: 'older2222222' }),
          mkJob({ id: 'active333333', done: false }),
        ]}
      />,
    );
    expect(html).toContain('data-testid="run-compare"');
    expect(html).toContain('data-testid="run-compare-a"');
    expect(html).toContain('data-testid="run-compare-b"');
    expect(html).toContain('data-testid="run-compare-mode"');
    expect(html).toContain('value="newest111111"');
    expect(html).toContain('value="older2222222"');
    expect(html).not.toContain('active333333');
  });

  it('hides with fewer than two finished runs', () => {
    expect(renderToStaticMarkup(<RunComparePanel jobs={[mkJob()]} />)).toBe('');
    expect(renderToStaticMarkup(<RunComparePanel jobs={[]} />)).toBe('');
  });
});

describe('job schedule button (issue #655)', () => {
  const noop = () => {};
  it('shows Schedule on finished non-destructive runs only', () => {
    const done = renderToStaticMarkup(
      <JobListItem job={mkJob()} repo="remote-server" onSelect={noop} onRerun={noop} onCancel={noop} onSchedule={noop} />,
    );
    expect(done).toContain('data-testid="job-schedule"');
    expect(done).toContain('>Schedule…<');
    const active = renderToStaticMarkup(
      <JobListItem job={mkJob({ done: false })} repo="remote-server" onSelect={noop} onRerun={noop} onCancel={noop} onSchedule={noop} />,
    );
    expect(active).not.toContain('data-testid="job-schedule"');
  });

  it('hides Schedule on destructive runs and run-td rows keep it', () => {
    const danger = renderToStaticMarkup(
      <JobListItem
        job={mkJob({ kind: 'recreate-code', name: 'recreate-code' })}
        repo="remote-server"
        onSelect={noop}
        onRerun={noop}
        onCancel={noop}
        onSchedule={noop}
      />,
    );
    expect(danger).not.toContain('data-testid="job-schedule"');
    const td = renderToStaticMarkup(
      <JobListItem
        job={mkJob({ kind: 'run-td', name: 'run-td', td_argv: ['td', 'context-warm'] })}
        repo="remote-server"
        onSelect={noop}
        onRerun={noop}
        onCancel={noop}
        onSchedule={noop}
      />,
    );
    expect(td).toContain('data-testid="job-schedule"');
  });
});

describe('triage fanout launcher (issue #670)', () => {
  const noop = () => {};
  it('offers a one-tap sweep for the current repo', () => {
    const html = renderToStaticMarkup(
      <TriageFanoutSection repo="boomtick" onDispatch={noop} />,
    );
    expect(html).toContain('data-testid="dispatch-triage-fanout"');
    expect(html).toContain('>Run triage fanout<');
    expect(html).toContain('boomtick');
  });

  it('finished fanout rows inherit re-run and scheduling', () => {
    const html = renderToStaticMarkup(
      <JobListItem
        job={mkJob({ kind: 'triage-fanout', name: 'triage-fanout' })}
        repo="boomtick"
        onSelect={noop}
        onRerun={noop}
        onCancel={noop}
        onSchedule={noop}
      />,
    );
    expect(html).toContain('data-testid="job-rerun"');
    expect(html).toContain('data-testid="job-schedule"');
    expect(html).not.toContain('asks for confirmation');
  });

  it('active fanout rows offer cancel', () => {
    const html = renderToStaticMarkup(
      <JobListItem
        job={mkJob({ kind: 'triage-fanout', name: 'triage-fanout', done: false })}
        repo="boomtick"
        onSelect={noop}
        onRerun={noop}
        onCancel={noop}
        onSchedule={noop}
      />,
    );
    expect(html).toContain('data-testid="job-cancel"');
  });
});

describe('ux audit launcher (issue #850)', () => {
  const noop = () => {};

  it('offers a site picker, page cap, and one-tap dispatch', () => {
    const html = renderToStaticMarkup(<UxAuditSection onDispatch={noop} />);
    expect(html).toContain('data-testid="ux-audit-site"');
    expect(html).toContain('data-testid="ux-audit-pages"');
    expect(html).toContain('data-testid="dispatch-ux-audit"');
    expect(html).toContain('>Run UX audit<');
    expect(html).toContain('arii.github.io');
    expect(html).toContain('boomtick.blog');
  });

  it('groups list shows the site and a refresh affordance', () => {
    const html = renderToStaticMarkup(<UxAuditGroups site="boomtick.blog" />);
    expect(html).toContain('Latest findings for boomtick.blog');
    expect(html).toContain('data-testid="ux-audit-refresh"');
  });

  it('finished audit rows inherit re-run and scheduling', () => {
    const html = renderToStaticMarkup(
      <JobListItem
        job={mkJob({ kind: 'ux-audit', name: 'ux-audit' })}
        repo="remote-server"
        onSelect={noop}
        onRerun={noop}
        onCancel={noop}
        onSchedule={noop}
      />,
    );
    expect(html).toContain('data-testid="job-rerun"');
    expect(html).toContain('data-testid="job-schedule"');
    expect(html).not.toContain('asks for confirmation');
  });

  it('active audit rows offer cancel', () => {
    const html = renderToStaticMarkup(
      <JobListItem
        job={mkJob({ kind: 'ux-audit', name: 'ux-audit', done: false })}
        repo="remote-server"
        onSelect={noop}
        onRerun={noop}
        onCancel={noop}
        onSchedule={noop}
      />,
    );
    expect(html).toContain('data-testid="job-cancel"');
  });
});

describe('jobs actionable rows (issue #907)', () => {
  const noop = () => {};

  it('collapses run output and offers copy', () => {
    const html = renderToStaticMarkup(
      <RunProgressPanel tail={'line one\nline two\n'} runId="abc" running={null} panelRef={{ current: null }} />,
    );
    expect(html).toContain('data-testid="jobs-output-wrap"');
    expect(html).toContain('<details');
    expect(html).toContain('data-testid="jobs-output-copy"');
    expect(html).toContain('Copy output');
  });

  it('offers copy on unified diffs', () => {
    const html = renderToStaticMarkup(<UnifiedDiffView diff={'-old\n+new\n'} />);
    expect(html).toContain('data-testid="run-compare-copy"');
    expect(html).toContain('Copy diff');
  });

  it('offers copy on open plans', () => {
    const html = renderToStaticMarkup(
      <PlanCardPanel
        card={{ repo: 'demo', name: 'plan.md' }}
        open
        content={'# plan\n'}
        onToggle={noop}
        onExecute={noop}
      />,
    );
    expect(html).toContain('data-testid="jobs-plan-copy"');
    expect(html).toContain('Copy plan');
  });

  it('links the empty state to Chat past runs', () => {
    const html = renderToStaticMarkup(
      <JobsListPanel
        list={[]}
        repo="demo"
        cursor={null}
        onSelect={noop}
        onRerun={noop}
        onCancel={noop}
        onSchedule={noop}
        onOpenChat={noop}
      />,
    );
    expect(html).toContain('data-testid="jobs-empty-chat"');
    expect(html).toContain('Open Chat past runs');
  });

  it('caps the list at the newest runs with a show-more control', () => {
    const list = Array.from({ length: 25 }, (_, i) =>
      mkJob({ id: `cap${String(i).padStart(12, '0')}` }),
    );
    const html = renderToStaticMarkup(
      <JobsListPanel
        list={list}
        repo="demo"
        cursor={null}
        onSelect={noop}
        onRerun={noop}
        onCancel={noop}
        onSchedule={noop}
      />,
    );
    expect(html).toContain('data-testid="jobs-show-more"');
    expect(html).toContain('Show 5 more runs');
    // Newest-first: the first 20 render, the tail stays behind the control.
    expect(html).toContain('job-cap000000000000');
    expect(html).not.toContain('job-cap000000000024');
  });

  it('renders a capped-exactly list with no show-more control', () => {
    const list = Array.from({ length: 20 }, (_, i) =>
      mkJob({ id: `cap${String(i).padStart(12, '0')}` }),
    );
    const html = renderToStaticMarkup(
      <JobsListPanel
        list={list}
        repo="demo"
        cursor={null}
        onSelect={noop}
        onRerun={noop}
        onCancel={noop}
        onSchedule={noop}
      />,
    );
    expect(html).not.toContain('data-testid="jobs-show-more"');
  });

  it('shows a loading indicator while findings load', () => {
    const html = renderToStaticMarkup(<UxAuditGroups site="buttons" />);
    expect(html).toContain('data-testid="ux-audit-groups-loading"');
    expect(html).toContain('Loading findings…');
  });
});

describe('jobs live feedback (issue #934)', () => {
  it('streams live output with a running indicator', () => {
    const html = renderToStaticMarkup(
      <RunProgressPanel tail={'step 1\n'} runId="abc123def456" running="triage fanout" panelRef={{ current: null }} />,
    );
    expect(html).toContain('data-testid="jobs-progress"');
    expect(html).toContain('data-live="true"');
    expect(html).toContain('data-testid="jobs-live-status"');
    expect(html).toContain('streaming live output');
    expect(html).toContain('Running triage fanout');
    expect(html).toContain('role="status"');
  });

  it('shows the live drawer immediately on trigger (empty tail, running set)', () => {
    const html = renderToStaticMarkup(
      <RunProgressPanel tail={''} runId="abc123def456" running="ux audit" panelRef={{ current: null }} />,
    );
    expect(html).toContain('data-testid="jobs-progress"');
    expect(html).toContain('Waiting for output…');
    expect(html).toContain('jobs-live-status');
  });

  it('hides the drawer when nothing has run', () => {
    const html = renderToStaticMarkup(
      <RunProgressPanel tail={''} runId={null} running={null} panelRef={{ current: null }} />,
    );
    expect(html).toBe('');
  });
});

describe('compare collapse (issue #1013)', () => {
  const twoDone = [mkJob(), mkJob({ id: 'b'.repeat(12) })];

  it('hides the section with fewer than two finished runs', () => {
    const html = renderToStaticMarkup(
      <CompareRunsSection jobs={[mkJob()]} open={true} onToggle={() => {}} />,
    );
    expect(html).toBe('');
  });

  it('renders only the toggle when closed (lazy: no diff pane)', () => {
    const html = renderToStaticMarkup(
      <CompareRunsSection jobs={twoDone} open={false} onToggle={() => {}} />,
    );
    expect(html).toContain('data-testid="run-compare-section"');
    expect(html).toContain('data-testid="run-compare-toggle"');
    expect(html).toContain('>Compare runs<');
    expect(html).not.toContain('data-testid="run-compare"');
  });

  it('mounts the compare pane when open', () => {
    const html = renderToStaticMarkup(
      <CompareRunsSection jobs={twoDone} open={true} onToggle={() => {}} />,
    );
    expect(html).toContain('data-testid="run-compare"');
    expect(html).toContain('>Hide compare<');
  });
});

describe('output drawer (issue #1013)', () => {
  it('renders nothing when closed', () => {
    const html = renderToStaticMarkup(
      <RunOutputDrawer
        open={false}
        onClose={() => {}}
        tail="out"
        runId="abc123def456"
        running={null}
        panelRef={{ current: null }}
      />,
    );
    expect(html).toBe('');
  });

  it('renders the output pane with a close control when open', () => {
    const html = renderToStaticMarkup(
      <RunOutputDrawer
        open={true}
        onClose={() => {}}
        tail=""
        runId="abc123def456"
        running="ux audit"
        panelRef={{ current: null }}
      />,
    );
    expect(html).toContain('data-testid="jobs-output-drawer"');
    expect(html).toContain('role="dialog"');
    expect(html).toContain('data-testid="jobs-output-close"');
    expect(html).toContain('data-testid="jobs-progress"');
  });
});
