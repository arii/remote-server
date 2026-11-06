import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import CoBrowsingDrawer, { bannerTextFor } from './CoBrowsingDrawer';

function renderDrawer(over: Record<string, unknown> = {}): string {
  return renderToStaticMarkup(
    <CoBrowsingDrawer
      browserState="IDLE"
      currentStep={null}
      iframeSrc="/browser/vnc.html?autoconnect=1&resize=scale"
      error={null}
      onPause={() => {}}
      onResume={() => {}}
      onClose={() => {}}
      {...(over as object)}
    />,
  );
}

describe('co-browsing drawer (follow_up Issue 11)', () => {
  it('banners each agent state in plain words', () => {
    expect(bannerTextFor('IDLE', null)).toBe('Shared browser ready');
    expect(bannerTextFor('RUNNING', null)).toContain('Agent active');
    expect(bannerTextFor('RUNNING', 'auditing')).toContain('auditing');
    expect(bannerTextFor('PAUSED_BY_USER', null)).toContain('Manual control active');
  });

  it('arms the takeover interceptor only while running', () => {
    expect(renderDrawer({ browserState: 'RUNNING' })).toContain('data-testid="cobrowse-takeover"');
    expect(renderDrawer({ browserState: 'PAUSED_BY_USER' })).not.toContain('cobrowse-takeover');
    expect(renderDrawer({ browserState: 'IDLE' })).not.toContain('cobrowse-takeover');
  });

  it('shows resume only while paused, and always the frame', () => {
    const paused = renderDrawer({ browserState: 'PAUSED_BY_USER', currentStep: 'Paused (user_takeover)' });
    expect(paused).toContain('data-testid="cobrowse-resume"');
    expect(paused).toContain('Resume agent');
    expect(paused).not.toContain('cobrowse-takeover');

    const running = renderDrawer({ browserState: 'RUNNING' });
    expect(running).not.toContain('cobrowse-resume');
    expect(running).toContain('data-testid="cobrowse-frame"');
    expect(running).toContain('src="/browser/vnc.html?autoconnect=1&amp;resize=scale"');
  });

  it('surfaces control API errors without dropping the frame', () => {
    const html = renderDrawer({ error: 'boom' });
    expect(html).toContain('data-testid="cobrowse-error"');
    expect(html).toContain('data-testid="cobrowse-frame"');
  });
});
