import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import JobSchedulesSection from './JobSchedulesSection';
import type { ActionSchedule } from '../apiClient';

function mkSchedule(over: Partial<ActionSchedule> = {}): ActionSchedule {
  return {
    id: 'abc123def456',
    action: 'logs',
    td_argv: [],
    repo: 'remote-server',
    cadence: 'daily',
    next_run: 1790000000,
    last_run: null,
    last_result: {},
    enabled: true,
    ...over,
  };
}

describe('schedule pause/resume (issue #870)', () => {
  it('shows Pause on enabled schedules, Resume + Paused chip on paused ones', () => {
    const html = renderToStaticMarkup(
      <JobSchedulesSection
        repo="remote-server"
        draft={null}
        initialSchedules={[mkSchedule({ id: 'aaa' }), mkSchedule({ id: 'bbb', enabled: false })]}
      />,
    );
    expect(html).toContain('data-testid="jobs-schedule-pause"');
    expect(html).toContain('>Pause schedule<');
    expect(html).toContain('data-testid="jobs-schedule-resume"');
    expect(html).toContain('>Resume schedule<');
    expect(html).toContain('data-testid="jobs-schedule-paused"');
    expect(html).toContain('>Paused<');
  });

  it('keeps the delete control beside the toggle', () => {
    const html = renderToStaticMarkup(
      <JobSchedulesSection repo="remote-server" draft={null} initialSchedules={[mkSchedule()]} />,
    );
    expect(html).toContain('data-testid="jobs-schedule-pause"');
    expect(html).toContain('data-testid="jobs-schedule-disable"');
    expect(html).not.toContain('data-testid="jobs-schedule-paused"');
  });
});
