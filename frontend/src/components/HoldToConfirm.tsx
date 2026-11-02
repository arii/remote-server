import { useEffect, useRef, useState } from 'react';

export interface HoldToConfirmProps {
  label: string;
  holdMs?: number;
  disabled?: boolean;
  onFire: () => void;
}

// Press-and-hold to arm, then tap to fire. Respects prefers-reduced-motion
// (arming is instant, no animated progress).
export default function HoldToConfirm({ label, holdMs = 800, disabled, onFire }: HoldToConfirmProps) {
  const [armed, setArmed] = useState(false);
  const [progress, setProgress] = useState(0);
  const timer = useRef<number | null>(null);
  const startRef = useRef(0);
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
    if ('key' in e && e.key !== ' ' && e.key !== 'Enter') return;
    if (reduced) {
      setArmed(true);
      setProgress(1);
      return;
    }
    startRef.current = Date.now();
    setProgress(0);
    clear();
    timer.current = window.setInterval(() => {
      const p = Math.min(1, (Date.now() - startRef.current) / holdMs);
      setProgress(p);
      if (p >= 1) {
        clear();
        setArmed(true);
      }
    }, 30);
  }

  function cancel() {
    if (armed) return;
    clear();
    setProgress(0);
  }

  return (
    <button
      type="button"
      disabled={disabled}
      aria-pressed={armed}
      aria-label={armed ? `Confirm ${label}` : `Hold to arm ${label}`}
      data-testid={armed ? 'hold-armed' : 'hold-disarmed'}
      onPointerDown={start}
      onPointerUp={cancel}
      onPointerLeave={cancel}
      onKeyDown={start}
      onKeyUp={(e) => {
        if ('key' in e && (e.key === ' ' || e.key === 'Enter') && !armed) cancel();
      }}
      onClick={() => {
        if (armed && !disabled) {
          setArmed(false);
          setProgress(0);
          onFire();
        }
      }}
    >
      {armed ? `Confirm ${label}` : label}
      <span aria-hidden="true" data-testid="hold-progress" data-progress={progress.toFixed(2)} />
    </button>
  );
}
