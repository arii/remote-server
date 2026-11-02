import { useEffect, useState } from 'react';
import { fetchSystemHealth, fetchWorkflowHealth } from '../apiClient';
import type {
  SystemHealthResponse,
  WorkflowHealthResponse,
  WorkflowRun,
} from '../apiClient';
import { loadSystemHealth, loadWorkflowHealth, useMock } from './mockViews';

function HealthLine({
  health,
  flow,
}: {
  health: SystemHealthResponse | null;
  flow: WorkflowHealthResponse | null;
}) {
  const healthText = health
    ? `${health.containers.length} container(s) reporting`
    : 'loading…';
  const flowText = flow
    ? ` · automated runs: ${flow.status} (${flow.total_failing} failing)`
    : '';
  return (
    <p className="row-meta" data-testid="observability-health">
      Stack health: {healthText}
      {flowText}
    </p>
  );
}

function RunRow({ run }: { run: WorkflowRun }) {
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
      {run.url ? <a href={run.url}>Open run trace</a> : null}
    </li>
  );
}

function RunsList({ flow }: { flow: WorkflowHealthResponse }) {
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
        <RunRow key={w.id} run={w} />
      ))}
    </ul>
  );
}

export default function ObservabilitySection({ repo }: { repo: string }) {
  const [health, setHealth] = useState<SystemHealthResponse | null>(null);
  const [flow, setFlow] = useState<WorkflowHealthResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    if (useMock()) {
      setHealth(loadSystemHealth());
      setFlow(loadWorkflowHealth());
      return;
    }
    (async () => {
      try {
        const [h, w] = await Promise.all([
          fetchSystemHealth(),
          fetchWorkflowHealth(repo),
        ]);
        if (!cancelled) {
          setHealth(h);
          setFlow(w);
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [repo]);

  if (error) {
    return (
      <section aria-label="System observability" data-testid="observability">
        <h2>System observability</h2>
        <div data-testid="observability-error" role="alert">
          Observability unavailable: {error}
        </div>
      </section>
    );
  }
  return (
    <section aria-label="System observability" data-testid="observability">
      <h2>System observability</h2>
      <HealthLine health={health} flow={flow} />
      {flow && <RunsList flow={flow} />}
    </section>
  );
}
