import { useEffect, useRef, useState } from 'react';

export interface TapToFireProps {
  label: string;
  disabled?: boolean;
  // Finality note for irreversible / session-loss actions. When set,
  // the first tap asks inline ("<label>? <confirm> [Confirm] [Cancel]")
  // instead of firing. No timing gesture either way.
  confirm?: string;
  onFire: () => void;
}

// Inline confirm prompt: "<label>? <consequence>".
export function confirmPrompt(label: string, consequence: string): string {
  return `${label}? ${consequence}`;
}

// Tap fires. Replaces the press-and-hold arm gesture on Jobs and agent
// surfaces, where timing gestures fail on flaky touch and some AT.
export default function TapToFire({ label, disabled, confirm, onFire }: TapToFireProps) {
  const [asking, setAsking] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const returnFocus = useRef(false);

  useEffect(() => {
    if (asking) confirmRef.current?.focus();
    else if (returnFocus.current) {
      returnFocus.current = false;
      triggerRef.current?.focus();
    }
  }, [asking]);

  function cancel() {
    returnFocus.current = true;
    setAsking(false);
  }

  function onConfirmKey(e: React.KeyboardEvent) {
    if (e.key === 'Escape') {
      e.preventDefault();
      cancel();
    }
  }

  if (asking && confirm !== undefined) {
    return (
      <span role="group" aria-label={confirmPrompt(label, confirm)} data-testid="tap-confirm">
        <span className="row-meta">{confirmPrompt(label, confirm)}</span>{' '}
        <button
          type="button"
          ref={confirmRef}
          aria-label={`Confirm ${label}`}
          onKeyDown={onConfirmKey}
          onClick={() => {
            setAsking(false);
            onFire();
          }}
        >
          Confirm
        </button>{' '}
        <button type="button" aria-label={`Cancel ${label}`} onKeyDown={onConfirmKey} onClick={cancel}>
          Cancel
        </button>
      </span>
    );
  }

  return (
    <button
      type="button"
      ref={triggerRef}
      disabled={disabled}
      aria-label={confirm === undefined ? label : `${label} (asks for confirmation)`}
      data-testid="tap-fire"
      onClick={() => {
        if (disabled) return;
        if (confirm === undefined) onFire();
        else setAsking(true);
      }}
    >
      {label}
    </button>
  );
}
