import TapToFire from '../components/TapToFire';
import type { ContainerStatsRow } from '../apiClient';
import { dangerEntryFor } from './DangerZone';

// Engine restart action per compose service (issue #663). Only
// services with a DangerZone action restart from the drawer —
// anything else has no privileged path and renders no button.
const RESTART_ACTION_FOR_SERVICE: Record<string, string> = {
  terminal: 'recreate',
  'code-server': 'recreate-code',
  buttons: 'deploy-buttons',
};

export function restartActionFor(service: string): string | null {
  return RESTART_ACTION_FOR_SERVICE[service] ?? null;
}

function cpuText(cpu: number | null): string {
  return cpu === null ? '—' : `${cpu}%`;
}

function memText(used: string | null, limit: string | null): string {
  return used && limit ? `${used} / ${limit}` : '—';
}

function ContainerRow({
  row,
  restartRequested,
  onRestart,
}: {
  row: ContainerStatsRow;
  restartRequested: string | null;
  onRestart: (action: string) => void;
}) {
  const action = restartActionFor(row.service);
  // No DangerZone entry, no button: a restart without the
  // verbatim confirm discipline must never dispatch.
  const entry = action ? dangerEntryFor(action) : null;
  return (
    <li className="detail" data-testid={`container-${row.service}`}>
      <span className="row-title">{row.name}</span>
      <p className="row-meta">
        {row.service} · {row.state} · CPU {cpuText(row.cpu_pct)} · RAM {memText(row.mem_used, row.mem_limit)}
      </p>
      {action && restartRequested === action ? (
        <p className="row-meta" data-testid={`container-restarted-${row.service}`}>
          restart requested — watch Jobs for the run.
        </p>
      ) : (
        action &&
        entry && (
          <div className="actions">
            <TapToFire
              label="Restart"
              confirm={entry.confirm}
              testid={`container-restart-${row.service}`}
              onFire={() => onRestart(action)}
            />
          </div>
        )
      )}
    </li>
  );
}

// Live per-container CPU/RAM with one-tap restart (issue #663).
// Rendered inline under the header while open; refreshes on open
// and on manual refresh only (no ticker, no streaming daemon).
export default function ContainerDrawer({
  rows,
  loading,
  error,
  restartRequested,
  restartError,
  onRestart,
  onRefresh,
  onClose,
}: {
  rows: ContainerStatsRow[];
  loading: boolean;
  error: string | null;
  restartRequested: string | null;
  restartError: string | null;
  onRestart: (action: string) => void;
  onRefresh: () => void;
  onClose: () => void;
}) {
  return (
    <section className="detail" data-testid="container-drawer" aria-label="Container health">
      <span className="row-title">Containers</span>
      {error && (
        <p role="alert" data-testid="container-error">
          Couldn&apos;t load container stats: {error}
        </p>
      )}
      {restartError && (
        <p role="alert" data-testid="container-restart-error">
          Couldn&apos;t start the restart: {restartError}
        </p>
      )}
      {!error && loading && (
        <p className="row-meta" role="status">
          Loading container stats…
        </p>
      )}
      {!error && !loading && rows.length === 0 && (
        <p className="row-meta" role="status">
          No containers reported.
        </p>
      )}
      {!error && !loading && rows.length > 0 && (
        <ul className="session-list">
          {rows.map((r) => (
            <ContainerRow key={r.name} row={r} restartRequested={restartRequested} onRestart={onRestart} />
          ))}
        </ul>
      )}
      <div className="actions">
        <button type="button" data-testid="container-refresh" onClick={onRefresh}>
          Refresh
        </button>{' '}
        <button type="button" data-testid="container-close" onClick={onClose}>
          Close
        </button>
      </div>
    </section>
  );
}
