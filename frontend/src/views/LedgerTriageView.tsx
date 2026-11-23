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

const RISK_LABELS: Record<string, string> = {
  production_mutation: 'production mutation',
  external_comms: 'external communication',
  billing_permission: 'billing or permission change',
};

function riskList(row: LedgerIntakeRow): string[] {
  return (row.risk_categories || []).map(String).filter(Boolean);
}

function rowId(row: LedgerIntakeRow): string {
  return String(row.intake_id || row.row_key || row.canonical_intake_id || '');
}

function statusLabel(status: string | undefined): string {
  return status === 'DUPLICATE' || status?.toUpperCase() === 'SUSPECTED-DUPLICATE'
    ? 'Suspected duplicate'
    : status || 'Needs review';
}

const REVIEWED_KEY = 'ledgerTriage.reviewed';

function rowTime(row: LedgerIntakeRow): number {
  const raw = row.ts ?? row.at;
  if (typeof raw === 'number') return raw * 1000;
  const parsed = raw ? Date.parse(String(raw)) : NaN;
  return Number.isNaN(parsed) ? 0 : parsed;
}

function newestFirst(rows: LedgerIntakeRow[]): LedgerIntakeRow[] {
  return [...rows].sort((a, b) => rowTime(b) - rowTime(a));
}

function loadReviewed(): Set<string> {
  try {
    const stored: unknown = JSON.parse(localStorage.getItem(REVIEWED_KEY) || '[]');
    return new Set(Array.isArray(stored) ? stored.map(String) : []);
  } catch {
    return new Set();
  }
}

const PROBLEM_PREVIEW_LENGTH = 160;

function LedgerProblem({ problem }: { problem?: string }) {
  if (!problem) return null;
  if (problem.length <= PROBLEM_PREVIEW_LENGTH) return <p>{problem}</p>;
  return (
    <details className="ledger-card-problem">
      <summary>{problem.slice(0, PROBLEM_PREVIEW_LENGTH).trimEnd()}…</summary>
      <p>{problem}</p>
    </details>
  );
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
  const [serverRisks, setServerRisks] = useState<Record<string, string[]>>({});
  const [mergeNotes, setMergeNotes] = useState<Record<string, string>>({});
  const [reviewed, setReviewed] = useState<Set<string>>(loadReviewed);
  const unreviewedCount = rows.filter((row) => !reviewed.has(rowId(row))).length;

  function markReviewed(id: string) {
    if (!id || reviewed.has(id)) return;
    const next = new Set(reviewed).add(id);
    setReviewed(next);
    try {
      localStorage.setItem(REVIEWED_KEY, JSON.stringify([...next]));
    } catch {
      // Storage can be full or blocked; the marker still clears for this visit.
    }
  }

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const response = await fetchLedgerIntake();
      setRows(newestFirst(response.rows));
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

  function rowRisks(row: LedgerIntakeRow): string[] {
    return Array.from(new Set([...riskList(row), ...(serverRisks[rowId(row)] || [])]));
  }

  async function approve(row: LedgerIntakeRow) {
    const id = rowId(row);
    markReviewed(id);
    if (!id || busy) return;
    setBusy(id);
    setNotice(null);
    try {
      await approveLedgerIntake(id, values[id] || {}, rowRisks(row).length > 0);
      setRows((current) => current.filter((candidate) => rowId(candidate) !== id));
      setNotice('Marked READY.');
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      // The server re-derives risk from fields typed here. Switch this row to the
      // confirm control so the reviewer can retry.
      const found = /confirmation required: high blast radius \(([^)]*)\)/.exec(message);
      if (found) {
        const names = found[1].split(',').map((name) => name.trim()).filter(Boolean);
        setServerRisks((current) => ({ ...current, [id]: names }));
        setNotice('This row touches a high blast radius area. Confirm to approve.');
      } else {
        setNotice(`Could not approve: ${message}`);
      }
    } finally {
      setBusy(null);
    }
  }

  async function merge(row: LedgerIntakeRow) {
    const id = rowId(row);
    markReviewed(id);
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
    markReviewed(id);
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
          <p className="row-meta" data-testid="ledger-unreviewed-count">
            {unreviewedCount} unreviewed
          </p>
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
          const risks = rowRisks(row).map((name) => RISK_LABELS[name] || name);
          const sourceUrl = row.source_url?.trim();
          return (
            <article
              className="ledger-triage-card"
              key={id}
              tabIndex={0}
              onClick={() => markReviewed(id)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') markReviewed(id);
              }}
            >
              <div className="ledger-card-head">
                <span className="status-badge">{statusLabel(row.status)}</span>
                {!reviewed.has(id) && (
                  <strong data-testid={`unreviewed-${id}`}>Unreviewed</strong>
                )}
                <span className="row-meta">{id}</span>
              </div>
              {row.duplicate_candidate ? (
                <div className="ledger-duplicate-pair">
                  <div className="ledger-duplicate-pane">
                    <strong>Incoming row</strong>
                    <h3>{row.title || 'Untitled intake row'}</h3>
                    <LedgerProblem problem={row.problem} />
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
                  <LedgerProblem problem={row.problem} />
                </>
              )}
              {sourceUrl && /^https?:\/\//i.test(sourceUrl) && (
                <p className="row-meta">
                  <a href={sourceUrl} target="_blank" rel="noopener noreferrer">{sourceUrl}</a>
                </p>
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
              {risks.length > 0 && row.status?.toUpperCase() !== 'DUPLICATE' && (
                <p role="note" className="row-meta">
                  High blast radius: {risks.join(', ')}. A human must confirm before this row can be READY.
                </p>
              )}
              {row.status?.toUpperCase() === 'DUPLICATE' ? (
                <p className="row-meta">Merge or mark as not a duplicate before approving.</p>
              ) : risks.length > 0 ? (
                <TapToFire
                  label="Approve and mark READY"
                  testid={`approve-high-risk-${id}`}
                  disabled={busy !== null}
                  confirm={`This row touches ${risks.join(', ')}. Mark READY so the queue may pick it up?`}
                  onFire={() => void approve(row)}
                />
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
