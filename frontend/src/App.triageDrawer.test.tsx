// @vitest-environment happy-dom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App';
import TriageDrawer from './views/TriageDrawer';

// #1491 finding 6: the Chat status strip opens the triage drawer OVER
// Chat. Chat must stay mounted (stream, draft, scroll survive) and the
// page must not switch to another view. Real mounts: static markup
// cannot observe the click.

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLDivElement | null = null;

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(() => new Promise(() => {})),
  );
});

afterEach(() => {
  act(() => {
    root?.unmount();
  });
  root = null;
  host?.remove();
  host = null;
  vi.unstubAllGlobals();
});

function mount(ui: React.ReactElement) {
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => {
    root!.render(ui);
  });
}

const q = (id: string) => document.querySelector(`[data-testid="${id}"]`);

describe('triage drawer over Chat (#1491)', () => {
  it('status strip opens the drawer while Chat stays mounted', () => {
    mount(<App />);
    expect(q('triage-drawer')).toBeNull();
    const chat = q('chat-view');
    expect(chat).not.toBeNull();
    act(() => {
      (q('status-strip') as HTMLElement).click();
    });
    expect(q('triage-drawer')).not.toBeNull();
    // Same DOM node: Chat was not unmounted and remounted.
    expect(q('chat-view')).toBe(chat);
    expect(q('now-view')).toBeNull();
  });

  it('Close dismisses the drawer and returns to the same Chat', () => {
    mount(<App />);
    const chat = q('chat-view');
    act(() => {
      (q('status-strip') as HTMLElement).click();
    });
    act(() => {
      (q('triage-close') as HTMLElement).click();
    });
    expect(q('triage-drawer')).toBeNull();
    expect(q('chat-view')).toBe(chat);
  });

  it('Escape (native cancel) closes the drawer once', () => {
    const onClose = vi.fn();
    mount(
      <TriageDrawer
        open
        section="queue"
        onSectionChange={() => {}}
        onClose={onClose}
        repo="remote-server"
        onAskAgent={() => {}}
        onOpenChat={() => {}}
        onOpenHistory={() => {}}
      />,
    );
    const dlg = q('triage-drawer') as HTMLDialogElement;
    expect(dlg.getAttribute('aria-modal')).toBe('true');
    act(() => {
      dlg.dispatchEvent(new Event('cancel', { cancelable: true }));
    });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
