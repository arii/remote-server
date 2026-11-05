import { useEffect, useState } from 'react';
import RetryableError from '../components/RetryableError';
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
}: {
  containers: ContainerStatus[];
  error?: string | null;
  // Opens the container stats drawer (#896): every pill navigates.
  onOpenStats?: (service?: string) => void;
}) {
  if (error) {
    return (
      <p className="row-meta" data-testid="observability-containers-error" role="alert">
        Container list unavailable: {error}
      </p>
    );
  }
  if (containers.length === 0) {
    return (
      <p className="row-meta" data-testid="observability-containers-empty">
        No containers reporting.
      </p>
    );
  }
  return (
    <ul className="pills" data-testid="observability-containers">
      {containers.map((c, i) => {
        const running = (c.state ?? '').toLowerCase() === 'running';
        const sick = (c.health ?? '').toLowerCase() === 'unhealthy';
        const label = `${c.service ?? c.name ?? 'container'}: ${c.state ?? '?'}${c.health ? ` (${c.health})` : ''}`;
        // eslint-disable-next-line react/no-array-index-key
        return (
          <li key={`${c.service ?? c.name ?? 'container'}-${i}`} data-testid="container-pill">
            <button
              type="button"
              className="repo-chip pill-dot"
              title={c.status ?? ''}
              aria-label={`${label} — open container logs`}
              onClick={() => onOpenStats?.(c.service ?? c.name ?? '')}
            >
              <span
                aria-hidden="true"
                className={running && !sick ? 'dot dot-ok' : 'dot dot-bad'}
              />
              {label}
            </button>
          </li>
        );
      })}
    </ul>
  );
}

function HealthLine({
  health,
  flow,
  onOpenStats,
}: {
  health: SystemHealthResponse | null;
  flow: WorkflowHealthResponse | null;
  onOpenStats?: (service?: string) => void;
}) {
  if (!health) {
    return (
      <p className="row-meta" data-testid="observability-health" role="status">
        Stack health: loading…
      </p>
    );
  }
  const up = health.containers.filter((c) => c.state === 'running').length;
  const flowText = flow
    ? ` · automated runs: ${flow.status} (${flow.total_failing} failing)`
    : '';
  const stackText = health.containers_error
    ? 'Stack health: container list unavailable'
    : `Stack health: ${up}/${health.containers.length} containers up`;
  return (
    <div data-testid="observability-health">
      <p className="row-meta">
        {stackText}
        {health.disk.summary ? ` · Disk: ${health.disk.summary}` : ''}
        {health.memory.summary ? ` · Mem: ${health.memory.summary}` : ''}
        {flowText}
      </p>
      <ContainerPills containers={health.containers} error={health.containers_error} onOpenStats={onOpenStats} />
    </div>
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
      <p className="row-meta" data-testid="observability-runs-empty">
        No failing automated runs — everything that runs automatically is
        healthy.
      </p>
    );
  }
  return (
    <ul className="session-list" data-testid="observability-runs">
      {flow.workflows.map((w) => (
        <RunRow key={w.id} run={w} onFindInFeed={onFindInFeed} />
      ))}
    </ul>
  );
}

export function ObservabilityBody({
  health,
  flow,
  error,
  flowLoading = false,
  onOpenStats,
  onRetry = () => {},
  onFindInFeed,
}: {
  health: SystemHealthResponse | null;
  flow: WorkflowHealthResponse | null;
  error: string | null;
  flowLoading?: boolean;
  onOpenStats?: (service?: string) => void;
  onRetry?: () => void;
  onFindInFeed?: (text: string) => void;
}) {
  return (
    <>
      <HealthLine health={health} flow={flow} onOpenStats={onOpenStats} />
      {error ? (
        <RetryableError
          testid="observability-error"
          message={`Automated runs unavailable: ${error}`}
          retryTestid="observability-retry"
          onRetry={onRetry}
        />
      ) : (
        <>
          {!flow && flowLoading && (
            <p className="row-meta" data-testid="observability-loading" role="status">
              Loading automated runs…
            </p>
          )}
          {flow && <RunsList flow={flow} onFindInFeed={onFindInFeed} />}
        </>
      )}
    </>
  );
}

export default function ObservabilitySection({
  repo,
  health,
  onOpenStats,
  onFindInFeed,
}: {
  repo: string;
  health: SystemHealthResponse | null;
  onOpenStats?: (service?: string) => void;
  onFindInFeed?: (text: string) => void;
}) {
  const [flow, setFlow] = useState<WorkflowHealthResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  async function loadFlow() {
    setLoading(true);
    setError(null);
    try {
      setFlow(await fetchWorkflowHealth(repo));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    let cancelled = false;
    setError(null);
    if (useMock()) {
      setFlow(loadWorkflowHealth());
      setLoading(false);
      return;
    }
    (async () => {
      try {
        const w = await fetchWorkflowHealth(repo);
        if (cancelled) return;
        setFlow(w);
        setLoading(false);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : String(err));
        setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [repo]);

  return (
    <section aria-label="System observability" data-testid="observability">
      <h2>System observability</h2>
      <ObservabilityBody
        health={health}
        flow={flow}
        error={error}
        flowLoading={loading}
        onOpenStats={onOpenStats}
        onRetry={() => void loadFlow()}
        onFindInFeed={onFindInFeed}
      />
    </section>
  );
}
