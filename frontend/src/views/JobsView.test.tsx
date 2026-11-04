import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { dangerEntryFor } from './DangerZone';
import {
  HighlightedLine,
  JobListItem,
  RunComparePanel,
  RunProgressPanel,
  SideBySideDiffView,
  TriageFanoutSection,
  UnifiedDiffView,
} from './JobsView';
import type { JobRecord } from '../apiTypes';

function mkJob(over: Partial<JobRecord> = {}): JobRecord {
  return { id: 'abc123def456', kind: 'logs', name: 'logs', repo: 'remote-server', done: true, ...over };
}

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
