import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { ContainerLogsPanel } from './ContainerLogsDrawer';

function renderPanel(over: Record<string, unknown> = {}): string {
  return renderToStaticMarkup(
    <ContainerLogsPanel
      service="caddy"
      name="remote-caddy-1"
      usage="running · Up 51 minutes · CPU 0.1% · RAM 9MiB / 1GiB"
      lines={['caddy: listening on :80', 'caddy: health check ok']}
      truncated={false}
      loading={false}
      error={null}
      canRestart
      restartNote={null}
      restartError={null}
      onRestart={() => {}}
      onRefresh={() => {}}
      onClose={() => {}}
      {...(over as object)}
    />,
  );
}

describe('container log slide-over (issue #928)', () => {
  it('renders usage, log lines, and actions', () => {
    const html = renderPanel();
    expect(html).toContain('data-testid="container-logs-drawer"');
    expect(html).toContain('remote-caddy-1');
    expect(html).toContain('data-testid="container-logs-usage"');
    expect(html).toContain('Up 51 minutes');
    expect(html).toContain('0.1%');
    expect(html).toContain('data-testid="container-logs-lines"');
    expect(html).toContain('listening on :80');
    expect(html).toContain('data-testid="container-logs-refresh"');
    expect(html).toContain('data-testid="container-logs-close"');
  });

  it('notes when the tail is truncated', () => {
    expect(renderPanel({ truncated: true })).toContain('Showing the last 2 lines.');
    expect(renderPanel()).not.toContain('Showing the last');
  });

  it('shows loading, empty, and error states', () => {
    expect(renderPanel({ loading: true, lines: [] })).toContain('Loading container logs…');
    const empty = renderPanel({ lines: [] });
    expect(empty).toContain('data-testid="container-logs-empty"');
    expect(empty).toContain('No log lines reported.');
    const failed = renderPanel({ error: 'boom' });
    expect(failed).toContain('data-testid="container-logs-error"');
    expect(failed).toContain('boom');
  });

  it('offers confirm-gated restart, hidden after dispatch', () => {
    const html = renderPanel();
    expect(html).toContain('data-testid="container-logs-restart-caddy"');
    expect(html).toContain('Restart container');
    expect(html).toContain('aria-label="Restart container (asks for confirmation)"');
    const done = renderPanel({ restartNote: 'restart requested — the container is restarting.' });
    expect(done).toContain('data-testid="container-logs-restarted-caddy"');
    expect(done).not.toContain('container-logs-restart-caddy"');
  });

  it('threads the service into the restart trigger', () => {
    const html = renderPanel({
      service: 'buttons',
      restartConfirm: 'The console will restart.',
    });
    expect(html).toContain('data-testid="container-logs-restart-buttons"');
    expect(html).toContain('aria-label="Restart container (asks for confirmation)"');
  });

  it('offers no restart when the service must not restart', () => {
    const html = renderPanel({ service: 'terminal', canRestart: false });
    expect(html).not.toContain('container-logs-restart-terminal');
  });

  it('shows restart failures without dropping lines', () => {
    const html = renderPanel({ restartError: 'compose blew up' });
    expect(html).toContain('data-testid="container-logs-restart-error"');
    expect(html).toContain('compose blew up');
    expect(html).toContain('listening on :80');
  });
});
