import TapToFire from '../components/TapToFire';
import type { ContainerStatsRow, SystemHealthResponse } from '../apiClient';
import { dangerEntryFor } from './DangerZone';
import { telemetryText } from './ObservabilitySection';

// Engine restart action per compose service (issue #663). Only
// services with a DangerZone action restart from the drawer —
// anything else has no privileged path and renders no button.
// Terminal is deliberately absent: restarting it drops live agent
// sessions with no resume path (#444).
const RESTART_ACTION_FOR_SERVICE: Record<string, string> = {
  'code-server': 'recreate-code',
  buttons: 'deploy-buttons',
};

export function restartActionFor(service: string): string | null {
  return RESTART_ACTION_FOR_SERVICE[service] ?? null;
}

// Plain compose restart (issue #928): every non-terminal service
// without an engine action restarts through the system endpoint.
// Terminal stays unoffered (#444); engine-mapped services keep
// their DangerZone path (pull + rebuild, not a plain restart).
export function serviceRestartable(service: string): boolean {
  return service !== '' && service !== 'terminal' && restartActionFor(service) === null;
}

function cpuText(cpu: number | null): string {
  return cpu === null ? '—' : `${cpu}%`;
}

function memText(used: string | null, limit: string | null): string {
  return used && limit ? `${used} / ${limit}` : '—';
}

function stateText(row: ContainerStatsRow): string {
  const health = (row.health ?? '').trim();
  return health ? `${row.state} (${health})` : row.state;
}

function uptimeText(status?: string): string {
  const s = (status ?? '').trim();
  return s ? s : '—';
}

// Drawer rows from the header health snapshot (issue #928): the
// same store that powers the History health banner, so the drawer
// opens instantly and never blanks on a stats 404 from a stale
// backend. CPU/RAM stay null until the live overlay lands.
export function statsRowsFromHealth(health: SystemHealthResponse | null): ContainerStatsRow[] {
  if (!health || health.containers_error) return [];
  return health.containers.map((c) => ({
    name: c.name ?? c.service ?? 'container',
    service: c.service ?? c.name ?? 'container',
    state: c.state ?? 'unknown',
    status: c.status ?? '',
    health: c.health ?? '',
    cpu_pct: null,
    mem_used: null,
    mem_limit: null,
  }));
}

// Overlay live stats onto health-derived rows, matched by
// service: CPU/RAM (and fresher status) win, health rows without
// a live match keep their state/uptime, live-only rows append.
export function mergeStatsRows(base: ContainerStatsRow[], live: ContainerStatsRow[]): ContainerStatsRow[] {
  const pending = new Map(live.map((r) => [r.service || r.name, r]));
  const merged = base.map((b) => {
    const l = pending.get(b.service || b.name);
    if (!l) return b;
    pending.delete(b.service || b.name);
    return {
      ...b,
      status: l.status ?? b.status,
      health: l.health ?? b.health,
      cpu_pct: l.cpu_pct,
      mem_used: l.mem_used,
      mem_limit: l.mem_limit,
    };
  });
  for (const l of pending.values()) merged.push({ ...l });
  return merged;
}

function ContainerRow({
  row,
  restartRequested,
  serviceRestarted,
  onRestart,
  onRestartService,
  onOpenLogs,
  onFilterHistory,
}: {
  row: ContainerStatsRow;
  restartRequested: string | null;
  serviceRestarted: string | null;
  onRestart: (action: string) => void;
  onRestartService: (service: string) => void;
  onOpenLogs: (service: string) => void;
  onFilterHistory?: (service: string) => void;
}) {
  const action = restartActionFor(row.service);
  // No DangerZone entry, no engine button: a restart without the
  // verbatim confirm discipline must never dispatch.
  const entry = action ? dangerEntryFor(action) : null;
  const plainRestart = serviceRestartable(row.service);
  return (
    <li className="detail" data-testid={`container-${row.service}`}>
      <span className="row-title">{row.name}</span>
      <p className="row-meta">
        {row.service} · {stateText(row)} · {uptimeText(row.status)} · CPU {cpuText(row.cpu_pct)} · RAM{' '}
        {memText(row.mem_used, row.mem_limit)}
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
      {plainRestart &&
        (serviceRestarted === row.service ? (
          <p className="row-meta" data-testid={`container-service-restarted-${row.service}`}>
            restart requested — the container is restarting.
          </p>
        ) : (
          <div className="actions">
            <TapToFire
              label="Restart"
              confirm={`The ${row.service} container will restart.`}
              testid={`container-restart-${row.service}`}
              onFire={() => onRestartService(row.service)}
            />
          </div>
        ))}
      <div className="actions">
        <button type="button" data-testid={`container-logs-${row.service}`} onClick={() => onOpenLogs(row.service)}>
          Logs
        </button>{' '}
        {onFilterHistory && (
          <button
            type="button"
            data-testid={`container-history-${row.service}`}
            aria-label={`Filter history to ${row.service}`}
            title="Show this container in the History feed"
            onClick={() => onFilterHistory(row.service)}
          >
            History
          </button>
        )}
      </div>
    </li>
  );
}

// Compact container health matrix (issues #663, #928): state,
// uptime, live CPU/RAM, and Logs/Restart per container. Rendered
// inline under the header while open; refreshes on open and on
// manual refresh only (no ticker, no streaming daemon). Rows come
// from the health snapshot with a live stats overlay; when the
// overlay fails the health rows still render with a degraded note.
// Host Disk/Mem ride along (#931): the drawer is the inspecting
// path for live telemetry now that History is a pure feed.
export default function ContainerDrawer({
  rows,
  loading,
  error,
  degraded,
  restartRequested,
  restartError,
  serviceRestarted,
  diskSummary,
  memSummary,
  onRestart,
  onRestartService,
  onOpenLogs,
  onFilterHistory,
  onRefresh,
  onClose,
}: {
  rows: ContainerStatsRow[];
  loading: boolean;
  error: string | null;
  degraded?: string | null;
  restartRequested: string | null;
  restartError: string | null;
  serviceRestarted?: string | null;
  diskSummary?: string | null;
  memSummary?: string | null;
  onRestart: (action: string) => void;
  onRestartService: (service: string) => void;
  onOpenLogs: (service: string) => void;
  onFilterHistory?: (service: string) => void;
  onRefresh: () => void;
  onClose: () => void;
}) {
  const hostLine = telemetryText(diskSummary ?? null, memSummary ?? null);
  return (
    <section className="detail" data-testid="container-drawer" aria-label="Container health">
      <span className="row-title">Containers</span>
      {hostLine && (
        <p className="row-meta" data-testid="container-host-telemetry">
          {hostLine}
        </p>
      )}
      {error && (
        <p role="alert" data-testid="container-error">
          Couldn&apos;t load container stats: {error}
        </p>
      )}
      {!error && degraded && (
        <p className="row-meta" data-testid="container-degraded" role="status">
          {degraded}
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
            <ContainerRow
              key={r.name}
              row={r}
              restartRequested={restartRequested}
              serviceRestarted={serviceRestarted ?? null}
              onRestart={onRestart}
              onRestartService={onRestartService}
              onOpenLogs={onOpenLogs}
              onFilterHistory={onFilterHistory}
            />
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
