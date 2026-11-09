import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import CoBrowsingDrawer, { bannerTextFor } from './CoBrowsingDrawer';

function renderDrawer(over: Record<string, unknown> = {}): string {
  return renderToStaticMarkup(
    <CoBrowsingDrawer
      browserState="IDLE"
      currentStep={null}
      iframeSrc="/cdp-stream/"
      error={null}
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

  it('has no takeover overlay in any state (server pauses on input)', () => {
    expect(renderDrawer({ browserState: 'RUNNING' })).not.toContain('cobrowse-takeover');
    expect(renderDrawer({ browserState: 'PAUSED_BY_USER' })).not.toContain('cobrowse-takeover');
    expect(renderDrawer({ browserState: 'IDLE' })).not.toContain('cobrowse-takeover');
  });

  it('names chat driving even with no pipeline run owning the wheel', () => {
    expect(bannerTextFor('IDLE', 'Browser drive: navigate https://x/')).toContain(
      'Agent driving the browser (navigate https://x/)',
    );
    // A human takeover still outranks a stale agent step.
    expect(
      bannerTextFor('PAUSED_BY_USER', 'Browser drive: click 10,20'),
    ).toContain('Manual control active');
  });

  it('labels the rail as the browser agent with a first-run hint', () => {
    const html = renderDrawer({});
    expect(html).toContain('data-testid="cobrowse-chat-hint"');
    expect(html).toContain('ask it to take');
    expect(html).toContain('aria-label="Browser agent');
    expect(html).toContain('no repo attached');
  });

  it('names a pending handoff above every other banner (#1097)', () => {
    expect(bannerTextFor('PAUSED_BY_USER', 'Paused (x)', 'login wall')).toBe(
      'Waiting for you — login wall',
    );
    expect(bannerTextFor('RUNNING', 'driving', 'mfa prompt')).toContain('mfa prompt');
  });

  it('offers Hand Back instead of resume while a handoff waits', () => {
    const waiting = renderDrawer({
      browserState: 'PAUSED_BY_USER',
      currentStep: 'Waiting for human handoff: login wall',
      handoffReason: 'login wall',
      handoffUrl: 'https://x/login',
      onHandBack: () => {},
    });
    expect(waiting).toContain('Waiting for you — login wall');
    expect(waiting).toContain('data-testid="cobrowse-handback"');
    expect(waiting).toContain('Hand back');
    expect(waiting).toContain('Stuck on: https://x/login');
    expect(waiting).not.toContain('cobrowse-resume');
  });

  it('keeps resume when paused with no handoff pending', () => {
    const paused = renderDrawer({
      browserState: 'PAUSED_BY_USER',
      handoffReason: null,
      onHandBack: () => {},
    });
    expect(paused).toContain('data-testid="cobrowse-resume"');
    expect(paused).not.toContain('cobrowse-handback');
  });

  it('shows resume only while paused, and always the frame', () => {
    const paused = renderDrawer({ browserState: 'PAUSED_BY_USER', currentStep: 'Paused (user_takeover)' });
    expect(paused).toContain('data-testid="cobrowse-resume"');
    expect(paused).toContain('Resume agent');

    const running = renderDrawer({ browserState: 'RUNNING' });
    expect(running).not.toContain('cobrowse-resume');
    expect(running).toContain('data-testid="cobrowse-frame"');
    expect(running).toContain('src="/cdp-stream/"');
  });

  it('surfaces control API errors without dropping the frame', () => {
    const html = renderDrawer({ error: 'boom' });
    expect(html).toContain('data-testid="cobrowse-error"');
    expect(html).toContain('data-testid="cobrowse-frame"');
  });

  it('gives the browser full height with the agent in the side rail', () => {
    const html = renderDrawer({});
    expect(html).toContain('data-testid="agent-drawer"');
    expect(html).toContain('data-testid="agent-provider"');
    expect(html).toContain('cobrowse-split');
    expect(html).toContain('cobrowse-chat');
  });

  it('covers a not-yet-loaded frame with a named connecting state', () => {
    const html = renderDrawer({});
    expect(html).toContain('data-testid="cobrowse-frame-status"');
    expect(html).toContain('Connecting to the shared browser');
    // The frame still mounts underneath so the load can proceed.
    expect(html).toContain('data-testid="cobrowse-frame"');
  });
});
