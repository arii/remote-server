import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import type { DispatchItem } from '../apiTypes';
import QueueView, { QueueDiscovery, QueueHeader, QueueRepoGroup, QueueRow } from './QueueView';

function mkItem(over: Partial<DispatchItem> = {}): DispatchItem {
  return { repo: 'remote-server', issue: 853, state: 'queued', detail: '', ...over };
}

const noop = () => {};

describe('queue row (issue #853)', () => {
  it('dispatches queued rows and retries terminal rows', () => {
    const queued = renderToStaticMarkup(
      <QueueRow item={mkItem()} gates={null} ready={null} onDispatch={noop} onCancel={noop} onGates={noop} />,
    );
    expect(queued).toContain('data-testid="queue-dispatch"');
    expect(queued).toContain('>Dispatch<');
    expect(queued).toContain('data-testid="queue-gates-check"');
    expect(queued).toContain('data-testid="queue-cancel"');
    const failed = renderToStaticMarkup(
      <QueueRow
        item={mkItem({ state: 'failed', detail: 'exit 3' })}
        gates={null}
        ready={null}
        onDispatch={noop}
        onCancel={noop}
        onGates={noop}
      />,
    );
    expect(failed).toContain('>Re-dispatch<');
    expect(failed).toContain('failed · exit 3');
  });

  it('offers no taps on running rows', () => {
    const running = renderToStaticMarkup(
      <QueueRow
        item={mkItem({ state: 'running' })}
        gates={null}
        ready={null}
        onDispatch={noop}
        onCancel={noop}
        onGates={noop}
      />,
    );
    expect(running).not.toContain('data-testid="queue-dispatch"');
    expect(running).not.toContain('data-testid="queue-cancel"');
    expect(running).not.toContain('data-testid="queue-gates-check"');
  });

  it('renders gate reasons when loaded', () => {
    const withGates = renderToStaticMarkup(
      <QueueRow
        item={mkItem()}
        gates={['repair worker is busy']}
        ready={false}
        onDispatch={noop}
        onCancel={noop}
        onGates={noop}
      />,
    );
    expect(withGates).toContain('repair worker is busy');
  });
});

describe('queue repo group (issue #853)', () => {
  it('offers a per-repo dispatch-next tap, disabled when nothing is queued', () => {
    const live = renderToStaticMarkup(
      <QueueRepoGroup
        group={{ repo: 'tech-dancer', items: [mkItem({ repo: 'tech-dancer', issue: 4848 })] }}
        gatesByKey={{}}
        readyByKey={{}}
        onDispatch={noop}
        onDispatchNext={noop}
        onCancel={noop}
        onGates={noop}
      />,
    );
    expect(live).toContain('data-testid="queue-dispatch-next-tech-dancer"');
    expect(live).toContain('data-testid="queue-group-tech-dancer"');
    expect(live).not.toContain('disabled');
    const idle = renderToStaticMarkup(
      <QueueRepoGroup
        group={{
          repo: 'tech-dancer',
          items: [mkItem({ repo: 'tech-dancer', issue: 4848, state: 'running' })],
        }}
        gatesByKey={{}}
        readyByKey={{}}
        onDispatch={noop}
        onDispatchNext={noop}
        onCancel={noop}
        onGates={noop}
      />,
    );
    expect(idle).toContain('disabled');
  });
});

describe('queue view loading (issue #853)', () => {
  it('renders the loading state before the first fetch', () => {
    const loading = renderToStaticMarkup(<QueueView repo="remote-server" />);
    expect(loading).toContain('data-testid="queue-loading"');
  });

  it('names the wait (issue #907)', () => {
    const loading = renderToStaticMarkup(<QueueView repo="remote-server" />);
    expect(loading).toContain('Loading dispatch queue…');
  });
});

describe('queue titles and gate badges (issue #882)', () => {
  it('renders the issue title beside repo#issue when present', () => {
    const titled = renderToStaticMarkup(
      <QueueRow
        item={mkItem({ title: 'Fix the flake' })}
        gates={null}
        ready={null}
        onDispatch={noop}
        onCancel={noop}
        onGates={noop}
      />,
    );
    expect(titled).toContain('remote-server#853 — Fix the flake');
    const untitled = renderToStaticMarkup(
      <QueueRow
        item={mkItem()}
        gates={null}
        ready={null}
        onDispatch={noop}
        onCancel={noop}
        onGates={noop}
      />,
    );
    expect(untitled).toContain('remote-server#853');
    expect(untitled).not.toContain('—');
  });

  it('badges ready/blocked rows and stays quiet without gates', () => {
    const ready = renderToStaticMarkup(
      <QueueRow
        item={mkItem()}
        gates={['Ready to dispatch.']}
        ready={true}
        onDispatch={noop}
        onCancel={noop}
        onGates={noop}
      />,
    );
    expect(ready).toContain('data-testid="queue-ready-remote-server-853"');
    expect(ready).toContain('>Ready<');
    const blocked = renderToStaticMarkup(
      <QueueRow
        item={mkItem()}
        gates={['repair worker is busy']}
        ready={false}
        onDispatch={noop}
        onCancel={noop}
        onGates={noop}
      />,
    );
    expect(blocked).toContain('data-testid="queue-blocked-remote-server-853"');
    expect(blocked).toContain('>Blocked<');
    const unknown = renderToStaticMarkup(
      <QueueRow
        item={mkItem()}
        gates={null}
        ready={null}
        onDispatch={noop}
        onCancel={noop}
        onGates={noop}
      />,
    );
    expect(unknown).not.toContain('queue-ready-remote-server-853');
    expect(unknown).not.toContain('queue-blocked-remote-server-853');
  });
});

describe('queue discovery (issue #882)', () => {
  const rows = [
    { repo: 'tech-dancer', number: 4848, title: 'Gates for lanes' },
    { repo: 'portfolio', number: 12, title: 'Refresh copy' },
  ];

  it('lists tagged open issues with per-row queue taps', () => {
    const html = renderToStaticMarkup(
      <QueueDiscovery issues={rows} queuedKeys={new Set()} onEnqueue={noop} />,
    );
    expect(html).toContain('data-testid="queue-discovery-list"');
    expect(html).toContain('data-testid="queue-discovery-repo-tech-dancer-4848"');
    expect(html).toContain('#4848 Gates for lanes');
    expect(html).toContain('data-testid="queue-discovery-queue-portfolio-12"');
  });

  it('marks already-queued rows instead of re-offering enqueue', () => {
    const html = renderToStaticMarkup(
      <QueueDiscovery issues={rows} queuedKeys={new Set(['tech-dancer#4848'])} onEnqueue={noop} />,
    );
    expect(html).toContain('data-testid="queue-discovery-queued-tech-dancer-4848"');
    expect(html).not.toContain('data-testid="queue-discovery-queue-tech-dancer-4848"');
    expect(html).toContain('data-testid="queue-discovery-queue-portfolio-12"');
  });

  it('reads empty when no issues are open', () => {
    const html = renderToStaticMarkup(
      <QueueDiscovery issues={[]} queuedKeys={new Set()} onEnqueue={noop} />,
    );
    expect(html).toContain('data-testid="queue-discovery-empty"');
  });
});

describe('queue dispatch-all (issue #882)', () => {
  it('offers dispatch-all with a count, disabled when nothing is queued', () => {
    const live = renderToStaticMarkup(
      <QueueHeader
        count={2}
        live={true}
        dispatchable={[mkItem(), mkItem({ repo: 'tech-dancer', issue: 1 })]}
        onRefresh={noop}
        onDispatchAll={noop}
      />,
    );
    expect(live).toContain('data-testid="queue-dispatch-all"');
    expect(live).toContain('Dispatch all (2)');
    expect(live).not.toContain('disabled');
    const idle = renderToStaticMarkup(
      <QueueHeader count={1} live={false} dispatchable={[]} onRefresh={noop} onDispatchAll={noop} />,
    );
    expect(idle).toContain('Dispatch all (0)');
    expect(idle).toContain('disabled');
  });
});

describe('queue completion loop (issue #883)', () => {
  it('renders titles from enqueue through terminal states', () => {
    const queued = renderToStaticMarkup(
      <QueueRow
        item={mkItem({ title: 'Fix the flake' })}
        gates={null}
        ready={null}
        onDispatch={noop}
        onCancel={noop}
        onGates={noop}
      />,
    );
    expect(queued).toContain('remote-server#853 — Fix the flake');
    const done = renderToStaticMarkup(
      <QueueRow
        item={mkItem({ state: 'done', title: 'Fix the flake' })}
        gates={null}
        ready={null}
        onDispatch={noop}
        onCancel={noop}
        onGates={noop}
      />,
    );
    expect(done).toContain('remote-server#853 — Fix the flake');
  });

  it('links terminal rows to the artifact in one tap', () => {
    const url = 'https://github.com/arii/remote-server/issues/853';
    const done = renderToStaticMarkup(
      <QueueRow
        item={mkItem({ state: 'done', title: 'Fix the flake', artifact_url: url })}
        gates={null}
        ready={null}
        onDispatch={noop}
        onCancel={noop}
        onGates={noop}
      />,
    );
    expect(done).toContain('data-testid="queue-artifact-remote-server-853"');
    expect(done).toContain(`href="${url}"`);
    expect(done).toContain('target="_blank"');
    const failed = renderToStaticMarkup(
      <QueueRow
        item={mkItem({ state: 'failed', detail: 'exit 3', artifact_url: url })}
        gates={null}
        ready={null}
        onDispatch={noop}
        onCancel={noop}
        onGates={noop}
      />,
    );
    expect(failed).toContain('data-testid="queue-artifact-remote-server-853"');
    expect(failed).toContain('failed · exit 3');
  });

  it('keeps live and linkless terminal rows as plain text', () => {
    const queued = renderToStaticMarkup(
      <QueueRow
        item={mkItem({ artifact_url: 'https://github.com/arii/remote-server/issues/853' })}
        gates={null}
        ready={null}
        onDispatch={noop}
        onCancel={noop}
        onGates={noop}
      />,
    );
    expect(queued).not.toContain('queue-artifact-remote-server-853');
    const legacy = renderToStaticMarkup(
      <QueueRow
        item={mkItem({ state: 'done' })}
        gates={null}
        ready={null}
        onDispatch={noop}
        onCancel={noop}
        onGates={noop}
      />,
    );
    expect(legacy).not.toContain('queue-artifact-remote-server-853');
    expect(legacy).toContain('remote-server#853');
  });
});
