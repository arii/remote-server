import { useEffect, useRef, useState } from 'react';

export interface InlineConfirmProps {
  label: string;
  confirmLabel?: string;
  cancelLabel?: string;
  prompt?: string;
  disabled?: boolean;
  onConfirm: () => void;
  'data-testid'?: string;
}

export default function InlineConfirm({
  label,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  prompt,
  disabled = false,
  onConfirm,
  'data-testid': testId,
}: InlineConfirmProps) {
  const [confirming, setConfirming] = useState(false);
  const containerRef = useRef<HTMLSpanElement | null>(null);

  useEffect(() => {
    if (!confirming) return;
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        e.preventDefault();
        setConfirming(false);
      }
    }
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [confirming]);

  if (!confirming) {
    return (
      <button
        type="button"
        disabled={disabled}
        data-testid={testId}
        onClick={() => setConfirming(true)}
      >
        {label}
      </button>
    );
  }

  return (
    <span
      ref={containerRef}
      className="inline-confirm"
      data-testid={testId ? `${testId}-inline` : 'inline-confirm'}
      role="group"
      aria-label={`Confirm ${label}`}
    >
      {prompt && (
        <span className="inline-confirm-prompt row-meta">
          {prompt}{' '}
        </span>
      )}
      <button
        type="button"
        className="inline-confirm-btn"
        data-testid={testId ? `${testId}-confirm` : 'inline-confirm-btn'}
        onClick={() => {
          setConfirming(false);
          onConfirm();
        }}
      >
        {confirmLabel}
      </button>
      {' '}
      <button
        type="button"
        className="inline-cancel-btn"
        data-testid={testId ? `${testId}-cancel` : 'inline-cancel-btn'}
        onClick={() => setConfirming(false)}
      >
        {cancelLabel}
      </button>
    </span>
  );
}
