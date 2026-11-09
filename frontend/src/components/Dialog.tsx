import { useEffect, useRef, type ReactNode } from 'react';

// Focus return (issue #1250): the trigger often never took focus
// (touch taps leave activeElement on body), so restoring prev alone
// lands on the document root. An explicit selector wins; otherwise
// the previous element, unless it was the body.
function resolveReturnFocus(
  selector: string | undefined,
  prev: Element | null,
  lookup: (s: string) => Element | null = (s) =>
    typeof document === 'undefined' ? null : document.querySelector(s),
): Element | null {
  if (selector) {
    const el = lookup(selector);
    if (el && (el as HTMLElement).focus instanceof Function) return el;
  }
  if (prev && prev !== (typeof document !== 'undefined' ? document.body : null)) return prev;
  return null;
}

// One shared wrapper on the native <dialog> element (issue #1128):
// showModal gives top-layer, Escape, and focus containment free;
// show() keeps the inline dialogs non-modal. Outside-tap dismiss,
// scroll-lock (modal only), and focus return live here once instead
// of hand-rolled per call site. Outside-close is required behavior
// (#1128 acceptance), not scope creep: Cancel/✕ already discard, so
// an outside tap discards nothing the UI promises to keep.
export default function Dialog({
  open,
  onClose,
  modal = true,
  label,
  testId,
  className,
  returnFocusTo,
  children,
}: {
  open: boolean;
  onClose: () => void;
  modal?: boolean;
  label: string;
  testId?: string;
  className?: string;
  returnFocusTo?: string;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  // Open the native dialog. Scroll-lock and focus return live in the
  // effect CLEANUP, so every close path restores them: open flipping
  // false while mounted (the component then renders null and the ref
  // is already detached, so the body could never restore — finding
  // 1), unmount while open, and modal changes. The previous inline
  // overflow is restored, not blanked, so a stacked modal underneath
  // keeps its lock. The el.open guard keeps StrictMode/dev double
  // effects (and modal flips while open) from re-showing.
  useEffect(() => {
    if (!open) return;
    const el = ref.current;
    if (!el || typeof el.showModal !== 'function' || el.open) return;
    const prevActive =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const prevOverflow = document.body.style.overflow;
    if (modal) {
      document.body.style.overflow = 'hidden';
      el.showModal();
    } else {
      el.show();
    }
    return () => {
      document.body.style.overflow = prevOverflow;
      (resolveReturnFocus(returnFocusTo, prevActive) as HTMLElement | null)?.focus?.();
    };
  }, [open, modal, returnFocusTo]);

  // Non-modal dialogs trap no focus, so an element-level Escape
  // handler only fires when focus happens to sit inside (finding 3).
  // One window listener owned here closes on Escape from anywhere;
  // key repeat is ignored so a held Escape cannot toggle a caller
  // back open. Modal dialogs rely on the native cancel event instead
  // (focus is always trapped inside) — exactly one notify path per
  // mode, so Escape can never double-notify (finding 2). The native
  // close event is never subscribed: nothing here calls .close(), and
  // unmounting an open dialog fires no close event.
  useEffect(() => {
    if (!open || modal) return;
    function onKey(e: KeyboardEvent) {
      if (e.key !== 'Escape' || e.repeat) return;
      e.preventDefault();
      onCloseRef.current();
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, modal]);

  // Non-modal dialogs have no backdrop: dismiss on outside tap via
  // one document listener owned here, not per call site.
  useEffect(() => {
    if (!open || modal) return;
    function onDown(e: MouseEvent) {
      const el = ref.current;
      if (el && e.target instanceof Node && !el.contains(e.target)) {
        onCloseRef.current();
      }
    }
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open, modal]);

  if (!open) return null;
  return (
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
    <dialog
      ref={ref}
      className={className}
      role="dialog"
      aria-label={label}
      aria-modal={modal || undefined}
      data-testid={testId}
      onCancel={(e) => {
        if (!modal) {
          // Swallowed: the window Escape listener above is the single
          // notify path for non-modal dialogs. Without this the
          // native cancel would notify a second time (finding 2).
          e.preventDefault();
          return;
        }
        // Modal: let the close through natively, then notify once —
        // the only notify path in modal mode.
        e.preventDefault();
        onCloseRef.current();
      }}
      onMouseDown={
        modal
          ? (e) => {
              // Backdrop taps target the dialog element itself.
              if (e.target === e.currentTarget) onCloseRef.current();
            }
          : undefined
      }
    >
      {children}
    </dialog>
  );
}
