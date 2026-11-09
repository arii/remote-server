// Co-browsing page: full-viewport shared browser (CDP-direct viewer
// at /cdp-stream via Caddy -> the headed session the agent drives)
// with the agent console riding the side rail. The viewer pauses the
// agent server-side on first human input, so no takeover overlay is
// needed. The rail is an agent session (muse/agy, repo-free), not
// the repo chat: browsing is agentic work. Presentational: the
// parent owns state polling and the resume POST, so this renders
// from props and stays testable via static markup like
// ContainerDrawer.
import { useEffect, useState } from 'react';
import type { AgentBrowserState } from '../apiClient';
import AgentDrawer from './AgentDrawer';

// The frame can sit blank for seconds (Grid slot wait, cold
// browser) or fail outright (Grid down, route 404): the overlay
// names the state and offers a retry instead of a black box.
type FrameState = 'loading' | 'live' | 'error';
const FRAME_LOAD_TIMEOUT_MS = 20_000;

export function bannerTextFor(
  state: AgentBrowserState,
  currentStep: string | null,
  handoffReason: string | null = null,
): string {
  // A pending agent-initiated handoff outranks every other banner: a
  // stalled wait must never look like a working session (#1097).
  if (handoffReason) return `Waiting for you — ${handoffReason}`;
  if (state === 'PAUSED_BY_USER') return 'Manual control active — agent paused';
  // Browser-drive ops record a "Browser drive:" step on the shared
  // control state (backend/api/routers/browser_drive.py contract):
  // show the agent at the wheel even when no pipeline run owns it.
  if (currentStep?.startsWith('Browser drive:')) {
    const what = currentStep.slice('Browser drive:'.length).trim();
    return `Agent driving the browser (${what}) — click the page to take over`;
  }
  if (state === 'RUNNING') {
    return currentStep
      ? `Agent active (${currentStep}) — select the browser view to take over`
      : 'Agent active — select the browser view to take over';
  }
  return 'Shared browser ready';
}

export default function BrowserView({
  browserState,
  currentStep,
  handoffReason = null,
  handoffUrl = null,
  iframeSrc,
  error,
  onResume,
  onHandBack = null,
}: {
  browserState: AgentBrowserState;
  currentStep: string | null;
  handoffReason?: string | null;
  handoffUrl?: string | null;
  iframeSrc: string;
  error: string | null;
  onResume: () => void;
  onHandBack?: (() => void) | null;
}) {
  const [frameState, setFrameState] = useState<FrameState>('loading');
  const [frameKey, setFrameKey] = useState(0);
  useEffect(() => {
    if (frameState !== 'loading') return;
    const timer = window.setTimeout(
      () => setFrameState('error'),
      FRAME_LOAD_TIMEOUT_MS,
    );
    return () => window.clearTimeout(timer);
  }, [frameState, frameKey]);
  function retryFrame() {
    setFrameKey((k) => k + 1);
    setFrameState('loading');
  }
  return (
    <section className="cobrowse-page" data-testid="cobrowse-drawer" aria-label="Shared browser">
      <header className="cobrowse-head">
        <div>
          <span className="row-title">Shared browser</span>{' '}
          <span className="row-meta" role="status" data-testid="cobrowse-status">
            {bannerTextFor(browserState, currentStep, handoffReason)}
          </span>
          {handoffReason && handoffUrl && (
            <span className="row-meta" data-testid="cobrowse-handoff-url">
              Stuck on: {handoffUrl}
            </span>
          )}
        </div>
        {error && (
          <p role="alert" data-testid="cobrowse-error">
            Couldn&apos;t reach the agent control API: {error}
          </p>
        )}
        <div className="actions">
          {handoffReason && onHandBack ? (
            <button type="button" data-testid="cobrowse-handback" onClick={onHandBack}>
              Hand back
            </button>
          ) : (
            browserState === 'PAUSED_BY_USER' && (
              <button type="button" data-testid="cobrowse-resume" onClick={onResume}>
                Resume agent
              </button>
            )
          )}{' '}
        </div>
      </header>
      <div className="cobrowse-split">
        <div className="cobrowse-frame-wrap">
          {frameState !== 'live' && (
            <div
              className="cobrowse-frame-status"
              role="status"
              data-testid="cobrowse-frame-status"
            >
              {frameState === 'loading' ? (
                <>
                  <span className="frame-spinner" aria-hidden="true" />
                  <p>Connecting to the shared browser…</p>
                </>
              ) : (
                <>
                  <p>The shared browser did not respond.</p>
                  <button type="button" data-testid="cobrowse-retry" onClick={retryFrame}>
                    Reconnect
                  </button>
                </>
              )}
            </div>
          )}
          <iframe
            key={frameKey}
            title="Shared browser head"
            src={iframeSrc}
            data-testid="cobrowse-frame"
            onLoad={() => setFrameState('live')}
          />
        </div>
        <aside className="cobrowse-chat" aria-label="Browser agent — ask it to drive the shared page">
          <p className="row-meta" data-testid="cobrowse-chat-hint">
            This agent sees and drives the page on the left — ask it to take
            you somewhere. It runs its own session, no repo attached. Click
            the page anytime to take over.
          </p>
          <AgentDrawer repo="" />
        </aside>
      </div>
    </section>
  );
}
