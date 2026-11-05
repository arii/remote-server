import { useEffect, useState } from 'react';
import TapToFire from '../components/TapToFire';
import { fetchContainerLogs } from '../apiClient';
import { loadContainerLogs, useMock } from './mockViews';

// Slide-over log tail for one container (issue #928): last 100
// lines of docker logs plus live resource usage and a restart
// button. One-shot fetch on open + manual refresh, never a
// streaming tail.
export function ContainerLogsPanel({
  service,
  name,
  usage,
  lines,
  truncated,
  loading,
  error,
  canRestart,
  restartConfirm,
  restartNote,
  restartError,
  onRestart,
  onRefresh,
  onClose,
}: {
  service: string;
  name: string;
  usage: string;
  lines: string[];
  truncated: boolean;
  loading: boolean;
  error: string | null;
  canRestart: boolean;
  restartConfirm?: string;
  restartNote: string | null;
  restartError: string | null;
  onRestart: (service: string) => void;
  onRefresh: () => void;
  onClose: () => void;
}) {
  return (
    <div className="logs-overlay" data-testid="container-logs-drawer">
      <section className="logs-panel" aria-label={`Logs for ${service}`}>
        <span className="row-title">
          {name} · logs
        </span>
        <p className="row-meta" data-testid="container-logs-usage">
          {usage}
        </p>
        {error && (
          <p role="alert" data-testid="container-logs-error">
            Couldn&apos;t load container logs: {error}
          </p>
        )}
        {restartError && (
          <p role="alert" data-testid="container-logs-restart-error">
            Couldn&apos;t start the restart: {restartError}
          </p>
        )}
        {restartNote && (
          <p className="row-meta" data-testid={`container-logs-restarted-${service}`} role="status">
            {restartNote}
          </p>
        )}
        {!error && loading && (
          <p className="row-meta" role="status">
            Loading container logs…
          </p>
        )}
        {!error && !loading && lines.length === 0 && (
          <p className="row-meta" data-testid="container-logs-empty" role="status">
            No log lines reported.
          </p>
        )}
        {!error && !loading && lines.length > 0 && (
          <>
            {truncated && (
              <p className="row-meta">Showing the last {lines.length} lines.</p>
            )}
            <pre className="row-meta logs-pre" data-testid="container-logs-lines">
              {lines.join('\n')}
            </pre>
          </>
        )}
        <div className="actions">
          {canRestart && !restartNote && (
            <>
              <TapToFire
                label="Restart container"
                confirm={restartConfirm ?? `The ${service} container will restart.`}
                testid={`container-logs-restart-${service}`}
                onFire={() => onRestart(service)}
              />{' '}
            </>
          )}
          <button type="button" data-testid="container-logs-refresh" onClick={onRefresh}>
            Refresh
          </button>{' '}
          <button type="button" data-testid="container-logs-close" onClick={onClose}>
            Close
          </button>
        </div>
      </section>
    </div>
  );
}

export default function ContainerLogsDrawer({
  service,
  name,
  usage,
  canRestart,
  restartConfirm,
  restartNote,
  restartError,
  onRestart,
  onClose,
}: {
  service: string;
  name: string;
  usage: string;
  canRestart: boolean;
  restartConfirm?: string;
  restartNote: string | null;
  restartError: string | null;
  onRestart: (service: string) => void;
  onClose: () => void;
}) {
  const [lines, setLines] = useState<string[]>([]);
  const [truncated, setTruncated] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [seq, setSeq] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    (async () => {
      try {
        const l = useMock() ? loadContainerLogs(service) : await fetchContainerLogs(service, 100);
        if (cancelled) return;
        setLines(l.lines);
        setTruncated(l.truncated);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [service, seq]);

  return (
    <ContainerLogsPanel
      service={service}
      name={name}
      usage={usage}
      lines={lines}
      truncated={truncated}
      loading={loading}
      error={error}
      canRestart={canRestart}
      restartConfirm={restartConfirm}
      restartNote={restartNote}
      restartError={restartError}
      onRestart={onRestart}
      onRefresh={() => setSeq((n) => n + 1)}
      onClose={onClose}
    />
  );
}
