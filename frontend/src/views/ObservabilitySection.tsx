import { useEffect, useState } from 'react';
import { fetchSystemHealth, fetchWorkflowHealth } from '../apiClient';
import type { SystemHealthResponse, WorkflowHealthResponse } from '../apiClient';
import { loadSystemHealth, loadWorkflowHealth, useMock } from './mockViews';

export default function ObservabilitySection({ mockKey }: { mockKey: number }) {
  const [health, setHealth] = useState<SystemHealthResponse | null>(null);
  const [flow, setFlow] = useState<WorkflowHealthResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        if (useMock()) {
          if (!cancelled) {
            setHealth(loadSystemHealth());
            setFlow(loadWorkflowHealth());
          }
          return;
        }
        const [h, w] = await Promise.all([
          fetchSystemHealth(),
          fetchWorkflowHealth('remote-server'),
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mockKey]);

  return (
    <section aria-label="System observability" data-testid="observability">
      <h2>System observability</h2>
      {error ? (
        <div data-testid="observability-error" role="alert">
          Observability unavailable: {error}
        </div>
      ) : (
        <>
          <p className="row-meta" data-testid="observability-health">
            Stack health:{' '}
            {health
              ? `${health.containers.length} container(s) reporting`
              : 'loading…'}
            {flow ? ` · automated runs: ${flow.status} (${flow.total_failing} failing)` : ''}
          </p>
          {flow && flow.workflows.length > 0 && (
            <ul className="session-list" data-testid="observability-runs">
              {flow.workflows.map((w) => (
                <li key={String(w.id)}>
                  <span className="row-title">
                    {String(w.name ?? 'Workflow')} failed on {String(w.branch ?? '?')}
                  </span>{' '}
                  <span className="row-meta">
                    {String(w.event ?? '')} · {String(w.diagnosed_cause ?? 'Workflow failed.')}
                  </span>{' '}
                  {w.url ? <a href={String(w.url)}>Open run trace</a> : null}
                </li>
              ))}
            </ul>
          )}
          {flow && flow.workflows.length === 0 && (
            <p className="row-meta" data-testid="observability-runs-empty">
              No failing automated runs — everything that runs automatically
              is healthy.
            </p>
          )}
        </>
      )}
    </section>
  );
}
