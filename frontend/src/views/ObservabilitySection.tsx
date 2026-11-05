import { useEffect, useState } from 'react';
import { fetchWorkflowHealth } from '../apiClient';
import type {
  ContainerStatus,
  SystemHealthResponse,
  WorkflowHealthResponse,
  WorkflowRun,
} from '../apiClient';
import { loadWorkflowHealth, useMock } from './mockViews';

export function ContainerPills({
  containers,
  error,
  onOpenStats,
  onFilterHistory,
}: {
  containers: ContainerStatus[];
  error?: string | null;
  // Opens the container stats drawer (#896): every pill navigates.
  onOpenStats?: (service?: string) => void;
  // Filters the History feed to the pill's container (#931): the
  // History tab owns the chronological feed, so the pill's second
  // action navigates there instead of duplicating state here.
  onFilterHistory?: (service: string) => void;
}) {
  if (error) {
    return (
      <p className="row-meta" data-testid="container-pills-error" role="alert">
        Container list unavailable: {error}
      </p>
    );
  }
  if (containers.length === 0) {
    return (
      <p className="row-meta" data-testid="container-pills-empty">
        No containers reporting.
      </p>
    );
  }
  return (
    <ul className="pills" data-testid="container-pills">
      {containers.map((c, i) => {
        const running = (c.state ?? '').toLowerCase() === 'running';
        const sick = (c.health ?? '').toLowerCase() === 'unhealthy';
        const service = c.service ?? c.name ?? '';
        const label = `${c.service ?? c.name ?? 'container'}: ${c.state ?? '?'}${c.health ? ` (${c.health})` : ''}`;
        // eslint-disable-next-line react/no-array-index-key
        return (
          <li key={`${c.service ?? c.name ?? 'container'}-${i}`} data-testid="container-pill">
            <button
              type="button"
              className="repo-chip pill-dot"
              title={c.status ?? ''}
              aria-label={`${label} — open container logs`}
              onClick={() => onOpenStats?.(service)}
            >
              <span
                aria-hidden="true"
                className={running && !sick ? 'dot dot-ok' : 'dot dot-bad'}
              />
              {label}
            </button>
            {onFilterHistory && (
              <button
                type="button"
                className="btn-link"
                data-testid={`container-history-${service}`}
                aria-label={`${label} — filter history`}
                title="Show this container in the History feed"
                onClick={() => onFilterHistory(service)}
              >
                History
              </button>
            )}
          </li>
        );
      })}
    </ul>
  );
}

// Host Disk/Mem telemetry line (#931): one copy source for Now's
// Containers section and the Status drawer. Null when neither
// summary exists, so callers render nothing instead of an empty
// line.
export function telemetryText(
  disk?: string | null,
  mem?: string | null,
): string | null {
  const parts: string[] = [];
  if (disk) parts.push(`Disk: ${disk}`);
  if (mem) parts.push(`Mem: ${mem}`);
  return parts.length > 0 ? parts.join(' · ') : null;
}

export function HostTelemetry({ health }: { health: SystemHealthResponse | null }) {
  if (!health) {
    return (
      <p className="row-meta" data-testid="host-telemetry" role="status">
        Host telemetry: loading…
      </p>
    );
  }
  const text = telemetryText(health.disk.summary, health.memory.summary);
  if (!text) return null;
  return (
    <p className="row-meta" data-testid="host-telemetry">
      {text}
    </p>
  );
}

export function RunRow({
  run,
  onFindInFeed,
}: {
  run: WorkflowRun;
  onFindInFeed?: (text: string) => void;
}) {
  const name = run.name ?? 'Workflow';
  const branch = run.branch ?? '?';
  const event = run.event ?? '';
  const cause = run.diagnosed_cause ?? 'Workflow failed.';
  return (
    <li key={run.id}>
      <span className="row-title">
        {name} failed on {branch}
      </span>{' '}
      <span className="row-meta">
        {event} · {cause}
      </span>{' '}
      {run.url ? (
        <a href={run.url} target="_blank" rel="noreferrer">
          Open run trace
        </a>
      ) : onFindInFeed ? (
        <button
          type="button"
          className="btn-secondary"
          data-testid={`observability-find-${run.id}`}
          onClick={() => onFindInFeed(name)}
        >
          Find in feed
        </button>
      ) : null}
    </li>
  );
}

function RunsList({
  flow,
  onFindInFeed,
}: {
  flow: WorkflowHealthResponse;
  onFindInFeed?: (text: string) => void;
}) {
  if (flow.workflows.length === 0) {
    return (
      <p className="row-meta" data-testid="workflow-runs-empty">
        No failing automated runs — everything that runs automatically is
        healthy.
      </p>
    );
  }
  return (
    <ul className="session-list" data-testid="workflow-runs">
      {flow.workflows.map((w) => (
        <RunRow key={w.id} run={w} onFindInFeed={onFindInFeed} />
      ))}
    </ul>
  );
}

// Failing-workflow body (#931): History keeps the chronological CI
// signal; live infra state (containers, Disk/Mem) lives in Now's
// Containers section and the Status drawer instead.
export function WorkflowRunsBody({
  flow,
  error,
}: {
  flow: WorkflowHealthResponse | null;
  error: string | null;
}) {
  if (error) {
    return (
      <div data-testid="workflow-runs-error" role="alert">
        Automated runs unavailable: {error}
      </div>
    );
  }
  if (!flow) {
    return (
      <p className="row-meta" data-testid="workflow-runs-loading" role="status">
        Loading automated runs…
      </p>
    );
  }
  return <RunsList flow={flow} />;
}

export function WorkflowRunsSection({ repo }: { repo: string }) {
  const [flow, setFlow] = useState<WorkflowHealthResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    if (useMock()) {
      setFlow(loadWorkflowHealth());
      return;
    }
    (async () => {
      try {
        const w = await fetchWorkflowHealth(repo);
        if (cancelled) return;
        setFlow(w);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [repo]);

  return (
    <section aria-label="Automated runs" data-testid="workflow-runs-section">
      <h2>Automated runs</h2>
      <WorkflowRunsBody flow={flow} error={error} />
    </section>
  );
}
