import { useEffect, useRef, useState } from 'react';

export interface TapToFireProps {
  label: string;
  disabled?: boolean;
  // Finality note for irreversible / session-loss actions. When set,
  // the first tap asks inline ("<label>? <confirm> [Confirm] [Cancel]")
  // instead of firing. No timing gesture either way.
  confirm?: string;
  // Per-instance trigger testid for repeated rows; defaults to tap-fire.
  testid?: string;
  // Accessible-name base for repeated rows (e.g. per-run Delete).
  // Replaces `label` in the trigger/confirm/cancel names while the
  // visible text stays `label`; defaults to `label`.
  name?: string;
  onFire: () => void;
}

// Inline confirm prompt: "<label>? <consequence>".
export function confirmPrompt(label: string, consequence: string): string {
  return `${label}? ${consequence}`;
}

// Tap fires. Replaces the press-and-hold arm gesture on Jobs and agent
// surfaces, where timing gestures fail on flaky touch and some AT.
export default function TapToFire({ label, disabled, confirm, testid = 'tap-fire', name, onFire }: TapToFireProps) {
  const spoken = name ?? label;
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
      <span role="group" aria-label={confirmPrompt(spoken, confirm)} data-testid="tap-confirm">
        <span className="row-meta">{confirmPrompt(spoken, confirm)}</span>{' '}
        <button
          type="button"
          ref={confirmRef}
          aria-label={`Confirm ${spoken}`}
          onKeyDown={onConfirmKey}
          onClick={() => {
            setAsking(false);
            onFire();
          }}
        >
          Confirm
        </button>{' '}
        <button type="button" aria-label={`Cancel ${spoken}`} onKeyDown={onConfirmKey} onClick={cancel}>
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
      aria-label={confirm === undefined ? spoken : `${spoken} (asks for confirmation)`}
      data-testid={testid}
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
