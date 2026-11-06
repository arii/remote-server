// Co-browsing page: full-viewport shared browser (CDP-direct viewer
// at /cdp-stream via Caddy -> the headed session the agent drives)
// with the chat console riding the side rail. The viewer pauses the
// agent server-side on first human input, so no takeover overlay is
// needed. Presentational: the parent owns state polling and the
// resume POST, so this renders from props and stays testable via
// static markup like ContainerDrawer.
import { useEffect, useState } from 'react';
import type { AgentBrowserState } from '../apiClient';
import ChatView from './ChatView';

// The frame can sit blank for seconds (Grid slot wait, cold
// browser) or fail outright (Grid down, route 404): the overlay
// names the state and offers a retry instead of a black box.
export type FrameState = 'loading' | 'live' | 'error';
const FRAME_LOAD_TIMEOUT_MS = 20_000;

export function bannerTextFor(state: AgentBrowserState, currentStep: string | null): string {
  if (state === 'PAUSED_BY_USER') return 'Manual control active — agent paused';
  if (state === 'RUNNING') {
    return currentStep
      ? `Agent active (${currentStep}) — select the browser view to take over`
      : 'Agent active — select the browser view to take over';
  }
  return 'Shared browser ready';
}

export default function CoBrowsingDrawer({
  browserState,
  currentStep,
  iframeSrc,
  repo,
  error,
  onResume,
  onClose,
}: {
  browserState: AgentBrowserState;
  currentStep: string | null;
  iframeSrc: string;
  repo: string;
  error: string | null;
  onResume: () => void;
  onClose: () => void;
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
            {bannerTextFor(browserState, currentStep)}
          </span>
        </div>
        {error && (
          <p role="alert" data-testid="cobrowse-error">
            Couldn&apos;t reach the agent control API: {error}
          </p>
        )}
        <div className="actions">
          {browserState === 'PAUSED_BY_USER' && (
            <button type="button" data-testid="cobrowse-resume" onClick={onResume}>
              Resume agent
            </button>
          )}{' '}
          <button type="button" data-testid="cobrowse-close" onClick={onClose}>
            Close
          </button>
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
        <aside className="cobrowse-chat" aria-label="Chat alongside the shared browser">
          <ChatView repo={repo} seed={null} onSeedUsed={() => {}} />
        </aside>
      </div>
    </section>
  );
}
