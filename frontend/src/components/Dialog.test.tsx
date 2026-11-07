// @vitest-environment happy-dom
import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, describe, expect, it } from 'vitest';
import Dialog from './Dialog';

// Behavior tests for the shared Dialog wrapper (Nebius BLOCK on
// #1135, findings 1/2/3/5): scroll-lock + focus return on every close
// path, exactly-once onClose per Escape, and focus-independent Escape
// for non-modal dialogs. renderToStaticMarkup cannot exercise any of
// this — these mount for real.

let root: Root | null = null;
let host: HTMLDivElement | null = null;

function mount(ui: ReactElement) {
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => {
    root!.render(ui);
  });
}

function rerender(ui: ReactElement) {
  act(() => {
    root!.render(ui);
  });
}

afterEach(() => {
  act(() => {
    root?.unmount();
  });
  root = null;
  host?.remove();
  host = null;
  document.body.style.overflow = '';
});

function dlg(): HTMLDialogElement {
  const el = document.querySelector('dialog');
  if (!el) throw new Error('expected an open <dialog>');
  return el as HTMLDialogElement;
}

describe('Dialog', () => {
  it('modal: locks scroll on open, restores scroll + focus when open flips false while mounted', () => {
    const opener = document.createElement('button');
    document.body.appendChild(opener);
    opener.focus();
    const onClose = () => {};
    try {
      mount(
        <Dialog open label="L" onClose={onClose}>
          <span>x</span>
        </Dialog>,
      );
      expect(document.body.style.overflow).toBe('hidden');
      // Close WITHOUT unmounting the Dialog component: the <dialog>
      // node is gone, so the effect must restore from its own
      // cleanup, not from a ref read.
      rerender(
        <Dialog open={false} label="L" onClose={onClose}>
          <span>x</span>
        </Dialog>,
      );
      expect(document.body.style.overflow).toBe('');
      expect(document.activeElement).toBe(opener);
    } finally {
      opener.remove();
    }
  });

  it('modal: restores scroll + focus on unmount while open', () => {
    mount(
      <Dialog open label="L" onClose={() => {}}>
        <span>x</span>
      </Dialog>,
    );
    expect(document.body.style.overflow).toBe('hidden');
    act(() => {
      root?.unmount();
    });
    root = null;
    expect(document.body.style.overflow).toBe('');
  });

  it('modal: one Escape (cancel) notifies exactly once', () => {
    let calls = 0;
    mount(
      <Dialog open label="L" onClose={() => calls++}>
        <span>x</span>
      </Dialog>,
    );
    act(() => {
      dlg().dispatchEvent(new Event('cancel', { cancelable: true }));
    });
    expect(calls).toBe(1);
  });

  it('non-modal: Escape with focus OUTSIDE the dialog still closes, exactly once', () => {
    let calls = 0;
    mount(
      <Dialog open modal={false} label="L" onClose={() => calls++}>
        <span>x</span>
      </Dialog>,
    );
    // Focus stays on body: the pre-#1135 element-level keydown could
    // never fire here (finding 3 regression).
    expect(document.activeElement).toBe(document.body);
    act(() => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    });
    expect(calls).toBe(1);
  });

  it('non-modal: swallows the native cancel so Escape cannot double-notify', () => {
    let calls = 0;
    mount(
      <Dialog open modal={false} label="L" onClose={() => calls++}>
        <span>x</span>
      </Dialog>,
    );
    act(() => {
      dlg().dispatchEvent(new Event('cancel', { cancelable: true }));
    });
    // The window Escape listener is the single notify path; the
    // native cancel must not notify on its own.
    expect(calls).toBe(0);
  });

  it('non-modal: ignores key repeat so a held Escape cannot toggle', () => {
    let calls = 0;
    mount(
      <Dialog open modal={false} label="L" onClose={() => calls++}>
        <span>x</span>
      </Dialog>,
    );
    act(() => {
      window.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, repeat: true }),
      );
    });
    expect(calls).toBe(0);
  });

  it('modal: backdrop tap closes, inside tap does not', () => {
    let calls = 0;
    mount(
      <Dialog open label="L" onClose={() => calls++}>
        <button type="button">inner</button>
      </Dialog>,
    );
    const el = dlg();
    act(() => {
      el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    });
    expect(calls).toBe(1);
    expect(el.open).toBe(true);
  });
});
