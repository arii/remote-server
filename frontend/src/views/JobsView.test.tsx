import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { dangerEntryFor } from './DangerZone';
import { JobListItem } from './JobsView';
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
        job={mkJob({ kind: 'recreate', name: 'recreate' })}
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
    expect(dangerEntryFor('recreate')).toEqual({ confirm: 'Live terminal sessions will drop.' });
    expect(dangerEntryFor('create-plan')).toEqual({});
    expect(dangerEntryFor('logs')).toBeNull();
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
