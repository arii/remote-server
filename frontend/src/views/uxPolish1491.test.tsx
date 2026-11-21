import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { JobListItem } from './JobsView';
import { ReportDialog } from './NowView';
import type { JobRecord } from '../apiTypes';

function row(extra: Partial<JobRecord>): string {
  const job = { id: 'j1', kind: 'check-notifications', repo: 'r', done: true, ...extra } as JobRecord;
  return renderToStaticMarkup(
    <ul>
      <JobListItem job={job} repo="r" onSelect={() => {}} onRerun={() => {}} onCancel={() => {}} />
    </ul>,
  );
}

describe('Jobs row outcome wording (#1491 finding 19)', () => {
  it('says failed with the exit code for a finished nonzero run', () => {
    const html = row({ exit_code: 1 });
    expect(html).toContain('failed (exit 1)');
    expect(html).not.toContain('done');
  });
  it('keeps done for a clean exit and active for a running job', () => {
    expect(row({ exit_code: 0 })).toContain('done');
    expect(row({ exit_code: 0 })).toContain('exit 0');
    const active = row({ done: false, exit_code: undefined });
    expect(active).toContain('active');
  });
});

describe('Report a bug submit (#1491 finding 15)', () => {
  it('is disabled while the title is empty', () => {
    const html = renderToStaticMarkup(
      <ReportDialog initialBody="" onSubmit={() => {}} onCancel={() => {}} />,
    );
    const btn = html.slice(html.indexOf('data-testid="report-submit"') - 40, html.indexOf('File bug'));
    expect(btn).toContain('disabled');
  });
});
