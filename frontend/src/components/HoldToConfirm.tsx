import { useEffect, useRef, useState } from 'react';

export interface HoldToConfirmProps {
  label: string;
  holdMs?: number;
  disabled?: boolean;
  onFire: () => void;
}

function buzz(ms: number) {
  if (typeof navigator !== 'undefined' && typeof navigator.vibrate === 'function') {
    navigator.vibrate(ms);
  }
}

// Pure tick decision for tests: tick when progress crosses into a new
// quartile (0.25 / 0.5 / 0.75). Completion (p >= 1) never ticks — the
// arm pulse replaces it. Reduced motion suppresses all ticks.
export function shouldTickHaptic(prevProgress: number, nextProgress: number, reduced: boolean): boolean {
  if (reduced) return false;
  if (nextProgress >= 1) return false;
  return Math.floor(nextProgress * 4) > Math.floor(prevProgress * 4);
}

// Firing requires a gesture separate from the arming press: the
// click that ends a mouse hold, or the tap that instantly arms under
// reduced motion, must never fire on its own. Only a later,
// deliberate tap confirms.
export const SEPARATE_GESTURE_MS = 300;

export function fireAllowed(armedAtMs: number, nowMs: number): boolean {
  return nowMs - armedAtMs >= SEPARATE_GESTURE_MS;
}

// Press-and-hold to arm, then tap to fire. Respects prefers-reduced-motion
// (arming is instant, no animated progress).
export default function HoldToConfirm({ label, holdMs = 800, disabled, onFire }: HoldToConfirmProps) {
  const [armed, setArmed] = useState(false);
  const [progress, setProgress] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const timer = useRef<number | null>(null);
  const startRef = useRef(0);
  const prevRef = useRef(0);
  const armedAtRef = useRef(0);
  const reduced =
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function clear() {
    if (timer.current !== null) {
      window.clearInterval(timer.current);
      timer.current = null;
    }
  }

  useEffect(() => clear, []);

  function start(e: React.PointerEvent | React.KeyboardEvent) {
    if (disabled || armed) return;
    if ('repeat' in e && e.repeat) return;
    if ('key' in e && e.key !== ' ' && e.key !== 'Enter') return;
    if (reduced) {
      armedAtRef.current = Date.now();
      setProgress(1);
      buzz(40);
      arm();
      return;
    }
    startRef.current = Date.now();
    prevRef.current = 0;
    setProgress(0);
    clear();
    buzz(10);
    timer.current = window.setInterval(() => {
      const p = Math.min(1, (Date.now() - startRef.current) / holdMs);
      if (shouldTickHaptic(prevRef.current, p, reduced)) buzz(10);
      prevRef.current = p;
      setProgress(p);
      if (p >= 1) {
        armedAtRef.current = Date.now();
        buzz(40);
        arm();
      }
    }, 30);
  }

  function cancel() {
    if (armed) return;
    clear();
    setProgress(0);
  }

  function disarm() {
    clear();
    setArmed(false);
    setProgress(0);
    setNotice(`${label} disarmed.`);
  }

  function arm() {
    clear();
    setArmed(true);
    setNotice(`${label} armed. Press Confirm to proceed, or Escape to cancel.`);
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Escape' && armed) {
      e.preventDefault();
      disarm();
      return;
    }
    if (e.key === ' ' || e.key === 'Enter') start(e);
  }

  // The control is a modeless inline confirm: it never traps focus or
  // overlays, so role=alertdialog would be wrong. Escape + announcements
  // below are the correct semantics for this pattern.
  return (
    <>
      <button
        type="button"
        disabled={disabled}
        aria-pressed={armed}
        aria-label={armed ? `Confirm ${label}` : `Hold to arm ${label}`}
        aria-keyshortcuts={armed ? 'Escape' : undefined}
        data-testid={armed ? 'hold-armed' : 'hold-disarmed'}
        onPointerDown={start}
        onPointerUp={cancel}
        onPointerLeave={cancel}
        onKeyDown={onKeyDown}
        onKeyUp={(e) => {
          if ('key' in e && (e.key === ' ' || e.key === 'Enter') && !armed) cancel();
        }}
        onClick={() => {
          if (armed && !disabled && fireAllowed(armedAtRef.current, Date.now())) {
            setArmed(false);
            setProgress(0);
            onFire();
          }
        }}
      >
        {armed ? `Confirm ${label}` : label}
        <span
          aria-hidden="true"
          data-testid="hold-progress"
          data-progress={progress.toFixed(2)}
          style={{ '--hold-p': progress.toFixed(3) } as React.CSSProperties}
        />
      </button>
      <span className="sr-only" role="status" data-testid="hold-notice">
        {notice}
      </span>
    </>
  );
}
