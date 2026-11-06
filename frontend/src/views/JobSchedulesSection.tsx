import { useEffect, useState } from 'react';
import {
  createActionSchedule,
  deleteActionSchedule,
  fetchActionSchedules,
  pauseActionSchedule,
  resumeActionSchedule,
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

// Bulk + grouping helpers (#1017): rows group by action in
// first-seen order; the armed delete confirm names its exact
// targets so Disable is never a blind single tap.
export interface ScheduleGroup {
  action: string;
  rows: ActionSchedule[];
}

export function groupSchedules(rows: ActionSchedule[]): ScheduleGroup[] {
  const groups = new Map<string, ActionSchedule[]>();
  for (const r of rows) {
    const argv = r.td_argv ?? [];
    const key = argv.length > 0 ? argv.join(' ') : r.action;
    const bucket = groups.get(key);
    if (bucket) bucket.push(r);
    else groups.set(key, [r]);
  }
  return Array.from(groups.entries()).map(([action, groupRows]) => ({
    action,
    rows: groupRows,
  }));
}

export function describeBulkTargets(rows: ActionSchedule[]): string {
  return rows
    .map((r) => {
      const argv = r.td_argv ?? [];
      return argv.length > 0 ? argv.join(' ') : r.action;
    })
    .join(', ');
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
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkArmed, setBulkArmed] = useState<'delete' | null>(null);
  const [bulkNote, setBulkNote] = useState<string | null>(null);
  const [busyOp, setBusyOp] = useState<string | null>(null);
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

  async function onPause(id: string) {
    setError(null);
    try {
      if (useMock()) return;
      await pauseActionSchedule(id);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function onResume(id: string) {
    setError(null);
    try {
      if (useMock()) return;
      await resumeActionSchedule(id);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  // Bulk run: sequential pause/resume/delete over a row set with
  // one notice naming failures (#1017). Single-flight via busyOp —
  // the per-row buttons below share the same guard.
  async function runBulk(
    op: 'pause' | 'resume' | 'delete',
    ids: string[],
  ) {
    if (busyOp !== null || ids.length === 0) return;
    setBusyOp(`bulk-${op}`);
    setBulkNote(null);
    setError(null);
    const failed: string[] = [];
    try {
      if (useMock()) return;
      for (const id of ids) {
        try {
          if (op === 'pause') await pauseActionSchedule(id);
          else if (op === 'resume') await resumeActionSchedule(id);
          else await deleteActionSchedule(id);
        } catch {
          failed.push(id.slice(0, 8));
        }
      }
      await load();
      if (op === 'delete') {
        setSelected(new Set());
        setBulkArmed(null);
      }
      setBulkNote(
        failed.length === 0
          ? `${op === 'delete' ? 'Deleted' : op === 'pause' ? 'Paused' : 'Resumed'} ${ids.length} schedule${ids.length === 1 ? '' : 's'}.`
          : `${op} partial: ${failed.length} failed (${failed.join(', ')}).`,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyOp(null);
    }
  }

  function toggleSelect(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  const list = schedules ?? [];
  const groups = groupSchedules(list);
  const selectedRows = list.filter((s) => selected.has(s.id));
  return (
    <section data-testid="jobs-schedule-section" aria-label="Scheduled runs">
      <h3>Scheduled runs</h3>
      <p className="row-meta">
        Non-destructive actions repeat on the shared tick: daily, weekly,
        or every 15 minutes.
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
              <option value="quarter-hourly">Every 15 minutes</option>
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
      {selectedRows.length > 0 && (
        <div className="actions" data-testid="jobs-schedule-bulkbar" role="group" aria-label="Bulk schedule actions">
          <span className="row-meta" data-testid="jobs-schedule-bulkcount">
            {selectedRows.length} selected
          </span>{' '}
          <button
            type="button"
            data-testid="jobs-schedule-bulk-pause"
            disabled={busyOp !== null}
            onClick={() => void runBulk('pause', selectedRows.map((s) => s.id))}
          >
            {busyOp === 'bulk-pause' ? 'Pausing…' : `Pause ${selectedRows.length}`}
          </button>{' '}
          <button
            type="button"
            data-testid="jobs-schedule-bulk-resume"
            disabled={busyOp !== null}
            onClick={() => void runBulk('resume', selectedRows.map((s) => s.id))}
          >
            {busyOp === 'bulk-resume' ? 'Resuming…' : `Resume ${selectedRows.length}`}
          </button>{' '}
          {bulkArmed === 'delete' ? (
            <>
              <span className="row-meta" data-testid="jobs-schedule-bulk-targets">
                Delete {describeBulkTargets(selectedRows)}?
              </span>{' '}
              <button
                type="button"
                data-testid="jobs-schedule-bulk-delete-confirm"
                disabled={busyOp !== null}
                onClick={() => void runBulk('delete', selectedRows.map((s) => s.id))}
              >
                {busyOp === 'bulk-delete' ? 'Deleting…' : 'Confirm delete'}
              </button>{' '}
              <button
                type="button"
                data-testid="jobs-schedule-bulk-delete-cancel"
                onClick={() => setBulkArmed(null)}
              >
                Cancel
              </button>
            </>
          ) : (
            <button
              type="button"
              data-testid="jobs-schedule-bulk-delete"
              disabled={busyOp !== null}
              onClick={() => setBulkArmed('delete')}
            >
              Delete {selectedRows.length}
            </button>
          )}
          {bulkNote && (
            <span className="row-meta" role="status" data-testid="jobs-schedule-bulk-note">
              {bulkNote}
            </span>
          )}
        </div>
      )}
      {groups.length > 0 && (
        <div data-testid="jobs-schedule-list">
          {groups.map((g) => (
            <section key={g.action} aria-label={`Schedules for ${g.action}`}>
              <p className="row-meta">
                <span data-testid={`jobs-schedule-group-${g.action}`}>
                  {g.action} ({g.rows.length})
                </span>{' '}
                <button
                  type="button"
                  data-testid={`jobs-schedule-group-pause-${g.action}`}
                  aria-label={`Pause all ${g.action} schedules`}
                  disabled={busyOp !== null || !g.rows.some((s) => s.enabled !== false)}
                  onClick={() => void runBulk('pause', g.rows.filter((s) => s.enabled !== false).map((s) => s.id))}
                >
                  Pause all
                </button>{' '}
                <button
                  type="button"
                  data-testid={`jobs-schedule-group-resume-${g.action}`}
                  aria-label={`Resume all ${g.action} schedules`}
                  disabled={busyOp !== null || !g.rows.some((s) => s.enabled === false)}
                  onClick={() => void runBulk('resume', g.rows.filter((s) => s.enabled === false).map((s) => s.id))}
                >
                  Resume all
                </button>
              </p>
              <ul className="session-list jobs-schedule-scroll">
                {g.rows.map((s) => (
                  <li key={s.id} className="detail">
                    <label className="row-meta" htmlFor={`jobs-schedule-select-${s.id}`}>
                      <input
                        id={`jobs-schedule-select-${s.id}`}
                        type="checkbox"
                        data-testid={`jobs-schedule-select-${s.id}`}
                        checked={selected.has(s.id)}
                        onChange={() => toggleSelect(s.id)}
                      />{' '}
                      Select
                    </label>{' '}
                    <span className="row-title">
                      {s.td_argv.length > 0 ? s.td_argv.join(' ') : s.action}
                    </span>{' '}
                    {s.enabled === false && (
                      <span className="row-meta" data-testid="jobs-schedule-paused">
                        Paused
                      </span>
                    )}
                    <p className="row-meta" data-testid="jobs-schedule-next">
                      {s.cadence} · {s.repo} · next{' '}
                      {new Date(s.next_run * 1000).toLocaleString()}
                      {s.last_result?.run_id ? ` · last ${s.last_result.run_id}` : ''}
                      {s.last_result?.ok === false && s.last_result?.error
                        ? ` · failed: ${s.last_result.error}`
                        : ''}
                    </p>
                    <details data-testid={`jobs-schedule-past-${s.id}`}>
                      <summary className="row-meta">Show past</summary>
                      <p className="row-meta">
                        {s.last_result?.run_id
                          ? `Last run ${s.last_result.run_id}: ${s.last_result.ok === false ? 'failed' : 'ok'}${s.last_result.error ? ` — ${s.last_result.error}` : ''}`
                          : 'No runs recorded yet.'}
                      </p>
                    </details>
                    <div className="actions">
                      {s.enabled === false ? (
                        <button
                          type="button"
                          data-testid="jobs-schedule-resume"
                          aria-label={`Resume schedule ${s.action} for ${s.repo}`}
                          disabled={busyOp !== null}
                          onClick={() => void onResume(s.id)}
                        >
                          Resume
                        </button>
                      ) : (
                        <button
                          type="button"
                          data-testid="jobs-schedule-pause"
                          aria-label={`Pause schedule ${s.action} for ${s.repo}`}
                          disabled={busyOp !== null}
                          onClick={() => void onPause(s.id)}
                        >
                          Pause
                        </button>
                      )}{' '}
                      <button
                        type="button"
                        data-testid="jobs-schedule-disable"
                        aria-label={`Disable schedule ${s.action} for ${s.repo}`}
                        disabled={busyOp !== null}
                        onClick={() => void onDisable(s.id)}
                      >
                        Disable
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      )}
      {error && (
        <p className="row-meta" role="alert">
          Schedule failed: {error}
        </p>
      )}
    </section>
  );
}
