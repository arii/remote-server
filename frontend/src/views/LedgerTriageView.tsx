import { useEffect, useState, type ReactElement } from 'react';
import {
  approveLedgerIntake,
  distinctLedgerIntake,
  fetchLedgerIntake,
  mergeLedgerIntake,
} from '../apiClient';
import TapToFire from '../components/TapToFire';
import type { LedgerIntakeRow } from '../apiTypes';

const FIELD_LABELS: Record<string, string> = {
  title: 'Title',
  problem: 'Problem',
  scope: 'Scope',
  done_when: 'Done-when',
};

function rowId(row: LedgerIntakeRow): string {
  return String(row.intake_id || row.row_key || row.canonical_intake_id || '');
}

function statusLabel(status: string | undefined): string {
  return status === 'DUPLICATE' || status?.toUpperCase() === 'SUSPECTED-DUPLICATE'
    ? 'Suspected duplicate'
    : status || 'Needs review';
}

function candidateIdOf(row: LedgerIntakeRow): string {
  const candidate = row.duplicate_candidate;
  return candidate
    ? String(candidate.canonical_intake_id || candidate.row_key || candidate.intake_id || '')
    : '';
}

export default function LedgerTriageView(): ReactElement {
  const [rows, setRows] = useState<LedgerIntakeRow[]>([]);
  const [values, setValues] = useState<Record<string, Record<string, string>>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [mergeNotes, setMergeNotes] = useState<Record<string, string>>({});

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const response = await fetchLedgerIntake();
      setRows(response.rows);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  function setField(id: string, field: string, value: string) {
    setValues((current) => ({
      ...current,
      [id]: { ...current[id], [field]: value },
    }));
  }

  async function approve(row: LedgerIntakeRow) {
    const id = rowId(row);
    if (!id || busy) return;
    setBusy(id);
    setNotice(null);
    try {
      await approveLedgerIntake(id, values[id] || {});
      setRows((current) => current.filter((candidate) => rowId(candidate) !== id));
      setNotice('Marked READY.');
    } catch (err) {
      setNotice(`Could not approve: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setBusy(null);
    }
  }

  async function merge(row: LedgerIntakeRow) {
    const id = rowId(row);
    const candidateId = candidateIdOf(row);
    if (!id || !candidateId || busy) return;
    setBusy(id);
    setNotice(null);
    try {
      await mergeLedgerIntake(id, candidateId, mergeNotes[id] || '');
      setRows((current) => current.filter((item) => rowId(item) !== id));
      setNotice('Merged as DUPLICATE after your confirmation.');
    } catch (err) {
      setNotice(`Could not merge: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setBusy(null);
    }
  }

  async function distinct(row: LedgerIntakeRow) {
    const id = rowId(row);
    const candidateId = candidateIdOf(row);
    if (!id || !candidateId || busy) return;
    setBusy(id);
    setNotice(null);
    try {
      await distinctLedgerIntake(id, candidateId);
      setRows((current) => current.filter((item) => rowId(item) !== id));
      setNotice('Marked distinct and reset to NEW.');
    } catch (err) {
      setNotice(`Could not mark distinct: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="ledger-triage" aria-labelledby="ledger-triage-title">
      <div className="page-head">
        <div>
          <h2 id="ledger-triage-title">Ledger triage</h2>
          <p className="row-meta">Review intake rows without editing the raw sheet.</p>
        </div>
        <button type="button" onClick={() => void load()} disabled={loading}>
          Refresh
        </button>
      </div>
      {loading && <p role="status">Loading intake…</p>}
      {error && <p role="alert">Intake unavailable: {error}</p>}
      {notice && <p role="status" className="row-meta">{notice}</p>}
      {!loading && !error && rows.length === 0 && (
        <p className="empty-state">No intake rows need triage.</p>
      )}
      <div className="ledger-card-deck">
        {rows.map((row) => {
          const id = rowId(row);
          const missing = (row.missing_fields || []).map(String);
          return (
            <article className="ledger-triage-card" key={id} tabIndex={0}>
              <div className="ledger-card-head">
                <span className="status-badge">{statusLabel(row.status)}</span>
                <span className="row-meta">{id}</span>
              </div>
              {row.duplicate_candidate ? (
                <div className="ledger-duplicate-pair">
                  <div className="ledger-duplicate-pane">
                    <strong>Incoming row</strong>
                    <h3>{row.title || 'Untitled intake row'}</h3>
                    {row.problem && <p>{row.problem}</p>}
                  </div>
                  <div className="ledger-duplicate-pane ledger-duplicate-candidate" aria-label="Duplicate candidate">
                    <strong>Possible match</strong>
                    <span>{row.duplicate_candidate.title || 'Untitled row'}</span>
                    <span className="row-meta">
                      {row.duplicate_candidate.status || 'Unknown status'} ·{' '}
                      {(row.duplicate_candidate.ts ?? row.duplicate_candidate.at)
                        ? new Date(
                            typeof (row.duplicate_candidate.ts ?? row.duplicate_candidate.at) === 'number'
                              ? Number(row.duplicate_candidate.ts ?? row.duplicate_candidate.at) * 1000
                              : String(row.duplicate_candidate.ts ?? row.duplicate_candidate.at),
                          ).toLocaleString()
                        : 'timestamp unavailable'}
                    </span>
                    <label>
                      Merge note (optional)
                      <input
                        value={mergeNotes[id] || ''}
                        onChange={(event) => setMergeNotes((current) => ({
                          ...current,
                          [id]: event.target.value,
                        }))}
                      />
                    </label>
                    <TapToFire
                      label="Merge duplicate"
                      name={`Merge duplicate ${row.title || id}`}
                      testid={`merge-${id}`}
                      confirm={`Link "${row.title || id}" to "${row.duplicate_candidate.title || 'matched row'}" and append notes. A human confirm is always required.`}
                      disabled={busy !== null}
                      onFire={() => void merge(row)}
                    />{' '}
                    <TapToFire
                      label="Not a duplicate"
                      name={`Not a duplicate ${row.title || id}`}
                      testid={`distinct-${id}`}
                      confirm={`Reset "${row.title || id}" to NEW and stop flagging it against "${row.duplicate_candidate.title || 'matched row'}".`}
                      disabled={busy !== null}
                      onFire={() => void distinct(row)}
                    />
                  </div>
                </div>
              ) : (
                <>
                  <h3>{row.title || 'Untitled intake row'}</h3>
                  {row.problem && <p>{row.problem}</p>}
                </>
              )}
              {row.duplicate_of && (
                <p className="row-meta">
                  Duplicate candidate: <a href={row.duplicate_of}>{row.duplicate_of}</a>
                </p>
              )}
              {missing.map((field) => (
                <label key={field}>
                  {FIELD_LABELS[field] || field}
                  <input
                    value={values[id]?.[field] ?? ''}
                    onChange={(event) => setField(id, field, event.target.value)}
                    aria-label={`${FIELD_LABELS[field] || field} for ${row.title || id}`}
                  />
                </label>
              ))}
              {row.status?.toUpperCase() === 'DUPLICATE' ? (
                <p className="row-meta">Merge or mark as not a duplicate before approving.</p>
              ) : (
                <button
                  type="button"
                  className="btn-primary"
                  onClick={() => void approve(row)}
                  disabled={busy !== null}
                >
                  {busy === id ? 'Saving…' : 'Approve and mark READY'}
                </button>
              )}
            </article>
          );
        })}
      </div>
    </section>
  );
}
