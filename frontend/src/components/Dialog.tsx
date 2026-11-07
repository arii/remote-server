import { useEffect, useRef, type ReactNode } from 'react';

// One shared wrapper on the native <dialog> element (issue #1128):
// showModal gives top-layer, Escape, and focus containment free;
// show() keeps the inline dialogs non-modal. Outside-tap dismiss,
// scroll lock (modal only), and focus return live here once instead
// of hand-rolled per call site.
export default function Dialog({
  open,
  onClose,
  modal = true,
  label,
  testId,
  className,
  children,
}: {
  open: boolean;
  onClose: () => void;
  modal?: boolean;
  label: string;
  testId?: string;
  className?: string;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement | null>(null);
  const returnTo = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    const el = ref.current;
    if (!el || typeof el.showModal !== 'function') return;
    if (open && !el.open) {
      returnTo.current =
        document.activeElement instanceof HTMLElement ? document.activeElement : null;
      if (modal) {
        document.body.style.overflow = 'hidden';
        el.showModal();
      } else {
        el.show();
      }
    } else if (!open && el.open) {
      el.close();
    }
    if (!open) {
      document.body.style.overflow = '';
      const back = returnTo.current;
      returnTo.current = null;
      back?.focus?.();
    }
  }, [open, modal]);

  // Unmount while open: never leave the page scroll-locked, and
  // hand focus back to the opener (the !open branch already
  // cleared returnTo, so a double restore is a no-op).
  useEffect(
    () => () => {
      document.body.style.overflow = '';
      const back = returnTo.current;
      returnTo.current = null;
      back?.focus?.();
    },
    [],
  );

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
  }, [open, modal ]);

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
        // Let the close through natively, then notify once.
        e.preventDefault();
        onCloseRef.current();
      }}
      onClose={() => onCloseRef.current()}
      onMouseDown={
        modal
          ? (e) => {
              // Backdrop taps target the dialog element itself.
              if (e.target === e.currentTarget) onCloseRef.current();
            }
          : undefined
      }
      onKeyDown={
        modal
          ? undefined
          : (e) => {
              if (e.key === 'Escape') {
                e.preventDefault();
                onCloseRef.current();
              }
            }
      }
    >
      {children}
    </dialog>
  );
}
