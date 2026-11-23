import { useEffect, useState, type ReactElement } from 'react';
import { approveLedgerIntake, fetchLedgerIntake } from '../apiClient';
import type { LedgerIntakeRow } from '../apiTypes';

const FIELD_LABELS: Record<string, string> = {
  title: 'Title',
  problem: 'Problem',
  scope: 'Scope',
  done_when: 'Done-when',
};

function rowId(row: LedgerIntakeRow): string {
  return String(row.canonical_intake_id || row.row_key || '');
}

function statusLabel(status: string | undefined): string {
  return status === 'DUPLICATE' || status?.toUpperCase() === 'SUSPECTED-DUPLICATE'
    ? 'Suspected duplicate'
    : status || 'Needs review';
}

export default function LedgerTriageView(): ReactElement {
  const [rows, setRows] = useState<LedgerIntakeRow[]>([]);
  const [values, setValues] = useState<Record<string, Record<string, string>>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

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
              <h3>{row.title || 'Untitled intake row'}</h3>
              {row.problem && <p>{row.problem}</p>}
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
              <button
                type="button"
                className="btn-primary"
                onClick={() => void approve(row)}
                disabled={busy !== null}
              >
                {busy === id ? 'Saving…' : 'Approve and mark READY'}
              </button>
            </article>
          );
        })}
      </div>
    </section>
  );
}
