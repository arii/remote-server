import { useEffect, useState } from 'react';
import type {
  AgentBrowserState,
  BrowserActivityEvent,
  BrowserTarget,
  BrowserTask,
  PairedBrowserState,
} from '../apiClient';
import {
  fetchBrowserActivity,
  fetchBrowserNotes,
  triggerBrowserLearn,
  fetchPairedBrowserState,
  browserTaskShotUrl,
  startAuthedInterval,
} from '../apiClient';
import AgentDrawer from './AgentDrawer';
import BrowserTasks from './BrowserTasks';

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

// Auto-learn panels: everything the system observes and remembers,
// visible in the tab where the browsing happens. Activity polls the
// shared ring (both sides' inputs, tagged human/agent/system); the
// notebook shows what learn passes wrote; Learn now runs the same
// deterministic pass on demand. Polls only while the document is
// visible — the tab stays mounted behind keepalive when hidden.
const LEARN_POLL_MS = 4000;

function ago(ts: number): string {
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  return `${Math.floor(m / 60)}h ago`;
}

function LearnPanels() {
  const [events, setEvents] = useState<BrowserActivityEvent[] | null>(null);
  const [notes, setNotes] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [learning, setLearning] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function refresh() {
      if (document.visibilityState === 'hidden') return;
      try {
        const [evts, nb] = await Promise.all([
          fetchBrowserActivity(30),
          fetchBrowserNotes(),
        ]);
        if (cancelled) return;
        setEvents(evts);
        setNotes(nb.notes);
        const learns = evts.filter((e) => e.kind === 'learn');
        setStatus(
          learns.length > 0
            ? `Last pass ${ago(learns[learns.length - 1].t)} · ${evts.length} recent events`
            : 'No pass yet — browse and one runs automatically',
        );
      } catch {
        if (!cancelled) setStatus('Could not reach the browser API.');
      }
    }
    void refresh();
    const stopPoll = startAuthedInterval(() => void refresh(), LEARN_POLL_MS);
    return () => {
      cancelled = true;
      stopPoll();
    };
  }, []);

  async function learnNow() {
    setLearning(true);
    try {
      const result = await triggerBrowserLearn();
      const [evts, nb] = await Promise.all([
        fetchBrowserActivity(30),
        fetchBrowserNotes(),
      ]);
      setEvents(evts);
      setNotes(nb.notes);
      setStatus(
        result.wrote
          ? `Learned ${result.events_covered ?? 0} events just now`
          : `Nothing new to learn (${result.reason ?? 'no new activity'})`,
      );
    } catch {
      setStatus('Learn pass failed — browser API unreachable.');
    } finally {
      setLearning(false);
    }
  }

  return (
    <>
      <section aria-label="Browser activity" data-testid="cobrowse-activity">
        <p className="row-meta">
          Activity — both sides, newest last{' '}
          <button
            type="button"
            data-testid="cobrowse-learn-now"
            onClick={() => void learnNow()}
            disabled={learning}
          >
            {learning ? 'Learning…' : 'Learn now'}
          </button>
        </p>
        <p className="row-meta" data-testid="cobrowse-learn-status">
          {status ?? 'Loading learning status…'}
        </p>
        {events === null ? (
          <p className="row-meta">Loading recent activity…</p>
        ) : events.length === 0 ? (
          <p className="row-meta">Nothing yet. Interact with the page.</p>
        ) : (
          <ol>
            {events.slice(-10).map((e, i) => (
              <li key={`${e.t}-${i}`} className="row-meta">
                {e.via} · {e.kind}
                {e.detail ? `: ${e.detail}` : ''} ({ago(e.t)})
              </li>
            ))}
          </ol>
        )}
      </section>
      <section aria-label="Browser notebook" data-testid="cobrowse-notes">
        <p className="row-meta">Notebook: what the system has learned</p>
        {notes === null ? (
          <p className="row-meta">Loading notebook…</p>
        ) : notes === '' ? (
          <p className="row-meta">Empty. The first pass will write here.</p>
        ) : (
          <pre className="row-meta cobrowse-notebook">{notes.slice(-2000)}</pre>
        )}
      </section>
    </>
  );
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
  const [target, setTarget] = useState<BrowserTarget>('grid');
  const [tasks, setTasks] = useState<BrowserTask[]>([]);
  const [paired, setPaired] = useState<PairedBrowserState | null>(null);
  const [pairError, setPairError] = useState<string | null>(null);
  const [pairRefresh, setPairRefresh] = useState(0);
  useEffect(() => {
    if (target !== 'paired') return;
    let active = true;
    setPaired(null);
    setPairError(null);
    void fetchPairedBrowserState().then((state) => { if (active) setPaired(state); }).catch((cause) => { if (active) setPairError(String(cause)); });
    return () => { active = false; };
  }, [target, pairRefresh]);
  const latest = tasks.find((task) => task.target === 'paired' && task.captures.length > 0);
  const latestShot = latest?.captures[latest.captures.length - 1];
  useEffect(() => {
    if (target !== 'grid' || frameState !== 'loading') return;
    const timer = window.setTimeout(
      () => setFrameState('error'),
      FRAME_LOAD_TIMEOUT_MS,
    );
    return () => window.clearTimeout(timer);
  }, [target, frameState, frameKey]);
  function retryFrame() {
    setFrameKey((k) => k + 1);
    setFrameState('loading');
  }
  return (
    <section className="cobrowse-page" data-testid="cobrowse-drawer" aria-label="Shared browser">
      <header className="cobrowse-head">
        <div>
          <label>Browser target{' '}<select aria-label="Browser target" value={target} onChange={(event) => { setTarget(event.target.value as BrowserTarget); setFrameState('loading'); }}><option value="grid">Shared browser</option><option value="paired">My Chrome</option></select></label>{' '}
          <span className="row-meta" role="status" data-testid="cobrowse-status">
            {target === 'grid' || browserState === 'PAUSED_BY_USER' ? bannerTextFor(browserState, currentStep, handoffReason) : 'Your Chrome, controlled through Buttons pair'}
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
      {target === 'paired' && <div className="row-meta">
        <span role="status">{paired === null ? (pairError ? 'Pairing unavailable' : 'Checking pairing…') : paired.connected ? 'Paired' : 'Not paired'}</span>{' '}
        <button type="button" onClick={() => setPairRefresh((value) => value + 1)}>Refresh pairing</button>
        {paired && !paired.connected && <p>Connect the extension using the <a href="https://github.com/arii/remote-server/blob/main/buttons-pair/README.md" target="_blank" rel="noreferrer">buttons-pair README</a>.</p>}
        {pairError && <p role="alert">{pairError}</p>}
      </div>}
      <div className="cobrowse-split">
        <div className="cobrowse-frame-wrap">
          {target === 'paired' ? (
            paired?.connected && latest && latestShot ? <figure className="browser-task-preview"><img src={browserTaskShotUrl(latest.id, latestShot.shot)} alt="Latest My Chrome task screenshot" /><figcaption className="row-meta">Latest task capture. Your Chrome is on your own screen.</figcaption></figure> : <p className="row-meta">{paired?.connected ? 'No task capture yet. Run Check to capture the page.' : 'Pair My Chrome to run tasks.'}</p>
          ) : <>
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
          </>}
        </div>
        <aside className="cobrowse-chat" aria-label={target === 'grid' ? 'Browser agent: ask it to drive the shared page' : 'My Chrome tasks'}>
          <BrowserTasks target={target} paired={paired?.connected ?? false} tasks={tasks} onChange={setTasks} />
          {target === 'grid' && <>
          <p className="row-meta" data-testid="cobrowse-chat-hint">
            This agent sees and drives the page on the left — ask it to take
            you somewhere. It runs its own session, no repo attached. Click
            the page anytime to take over.
          </p>
          <AgentDrawer repo="" />
          <LearnPanels />
          </>}
        </aside>
      </div>
    </section>
  );
}
