import type { HealthStatus } from '../types';

// Single health widget for the whole app (per #204: one widget, no duplicates).
export default function SystemHealth({ health }: { health: HealthStatus }) {
  return (
    <section data-testid="system-health" className="health" aria-label="System health">
      <span className={health.ok ? 'dot dot-ok' : 'dot dot-bad'} aria-hidden="true" />
      <span>{health.ok ? 'Healthy' : 'Degraded'}</span>
      <span className="row-meta">
        {health.sessions} session{health.sessions === 1 ? '' : 's'}
      </span>
    </section>
  );
}
