// Co-browsing page: full-viewport shared browser (CDP-direct viewer
// at /cdp-stream via Caddy -> the headed session the agent drives)
// with the chat console riding the side rail. The viewer pauses the
// agent server-side on first human input, so no takeover overlay is
// needed. Presentational: the parent owns state polling and the
// resume POST, so this renders from props and stays testable via
// static markup like ContainerDrawer.
import type { AgentBrowserState } from '../apiClient';
import ChatView from './ChatView';

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
          <iframe title="Shared browser head" src={iframeSrc} data-testid="cobrowse-frame" />
        </div>
        <aside className="cobrowse-chat" aria-label="Chat alongside the shared browser">
          <ChatView repo={repo} seed={null} onSeedUsed={() => {}} />
        </aside>
      </div>
    </section>
  );
}
