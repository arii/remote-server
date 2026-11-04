import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import type { DispatchItem } from '../apiTypes';
import QueueView, { QueueRepoGroup, QueueRow } from './QueueView';

function mkItem(over: Partial<DispatchItem> = {}): DispatchItem {
  return { repo: 'remote-server', issue: 853, state: 'queued', detail: '', ...over };
}

const noop = () => {};

describe('queue row (issue #853)', () => {
  it('dispatches queued rows and retries terminal rows', () => {
    const queued = renderToStaticMarkup(
      <QueueRow item={mkItem()} gates={null} onDispatch={noop} onCancel={noop} onGates={noop} />,
    );
    expect(queued).toContain('data-testid="queue-dispatch"');
    expect(queued).toContain('>Dispatch<');
    expect(queued).toContain('data-testid="queue-gates-check"');
    expect(queued).toContain('data-testid="queue-cancel"');
    const failed = renderToStaticMarkup(
      <QueueRow
        item={mkItem({ state: 'failed', detail: 'exit 3' })}
        gates={null}
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
});
