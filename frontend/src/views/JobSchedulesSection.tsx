import { useEffect, useState } from 'react';
import {
  createActionSchedule,
  deleteActionSchedule,
  fetchActionSchedules,
} from '../apiClient';
import type { ActionSchedule } from '../apiClient';
import { useMock } from './mockViews';

// Prefill carried from a finished run's Schedule control: the same
// #630 replay params the re-run button launches with.
export interface ScheduleDraft {
  action: string;
  td_argv: string[];
  repo: string;
}

export function draftLabel(draft: ScheduleDraft): string {
  if (draft.td_argv.length > 0) return draft.td_argv.join(' ');
  return draft.action;
}

export default function JobSchedulesSection({
  repo,
  draft,
  initialSchedules,
  initialDraft,
  onScheduled,
  onClearDraft,
}: {
  repo: string;
  // Live prefill from a finished run (null when none picked).
  draft: ScheduleDraft | null;
  initialSchedules?: ActionSchedule[];
  initialDraft?: ScheduleDraft | null;
  onScheduled?: (schedule: ActionSchedule) => void;
  onClearDraft?: () => void;
}) {
  const [schedules, setSchedules] = useState<ActionSchedule[] | null>(
    initialSchedules ?? null,
  );
  const [cadence, setCadence] = useState('daily');
  const [error, setError] = useState<string | null>(null);
  const activeDraft = draft ?? initialDraft ?? null;

  async function load() {
    try {
      setSchedules((await fetchActionSchedules()).schedules);
    } catch {
      // Schedules stay hidden when the endpoint is unreachable.
    }
  }

  useEffect(() => {
    if (useMock()) return;
    if (initialSchedules !== undefined) return;
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repo]);

  async function onConfirmDraft() {
    if (!activeDraft) return;
    setError(null);
    try {
      if (useMock()) return;
      const created = await createActionSchedule({
        action: activeDraft.action,
        td_argv: activeDraft.td_argv,
        repo: activeDraft.repo || repo,
        cadence,
      });
      await load();
      if (created.schedule) onScheduled?.(created.schedule);
      onClearDraft?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function onDisable(id: string) {
    setError(null);
    try {
      if (useMock()) return;
      await deleteActionSchedule(id);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  const list = schedules ?? [];
  return (
    <section data-testid="jobs-schedule-section" aria-label="Scheduled runs">
      <h3>Scheduled runs</h3>
      <p className="row-meta">
        Non-destructive actions repeat daily or weekly on the shared tick.
      </p>
      {activeDraft && (
        <div className="actions">
          <span className="row-title" data-testid="jobs-schedule-draft">
            {draftLabel(activeDraft)}
          </span>{' '}
          <label className="row-meta" htmlFor="jobs-schedule-cadence">
            Repeat{' '}
            <select
              id="jobs-schedule-cadence"
              data-testid="jobs-schedule-cadence"
              value={cadence}
              onChange={(e) => setCadence(e.target.value)}
            >
              <option value="daily">Daily</option>
              <option value="weekly">Weekly</option>
            </select>
          </label>{' '}
          <button
            type="button"
            data-testid="jobs-schedule-confirm"
            onClick={() => void onConfirmDraft()}
          >
            Confirm schedule
          </button>{' '}
          <button
            type="button"
            data-testid="jobs-schedule-cancel"
            onClick={() => onClearDraft?.()}
          >
            Cancel
          </button>
        </div>
      )}
      {list.length === 0 && !activeDraft && (
        <p className="row-meta" data-testid="jobs-schedule-empty">
          No scheduled runs. Finished runs offer Schedule to repeat them.
        </p>
      )}
      {list.length > 0 && (
        <ul className="session-list" data-testid="jobs-schedule-list">
          {list.map((s) => (
            <li key={s.id} className="detail">
              <span className="row-title">
                {s.td_argv.length > 0 ? s.td_argv.join(' ') : s.action}
              </span>
              <p className="row-meta" data-testid="jobs-schedule-next">
                {s.cadence} · {s.repo} · next{' '}
                {new Date(s.next_run * 1000).toLocaleString()}
                {s.last_result?.run_id ? ` · last ${s.last_result.run_id}` : ''}
                {s.last_result?.ok === false && s.last_result?.error
                  ? ` · failed: ${s.last_result.error}`
                  : ''}
              </p>
              <div className="actions">
                <button
                  type="button"
                  data-testid="jobs-schedule-disable"
                  onClick={() => void onDisable(s.id)}
                >
                  Disable schedule
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
      {error && (
        <p className="row-meta" role="alert">
          Schedule failed: {error}
        </p>
      )}
    </section>
  );
}
