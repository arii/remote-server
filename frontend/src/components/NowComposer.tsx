import { useEffect, useRef } from 'react';

export default function NowComposer({
  prompt,
  onChange,
  onSend,
  focusRequest,
  blockedReason,
}: {
  prompt: string;
  onChange: (text: string) => void;
  onSend: () => void;
  focusRequest: number;
  blockedReason: string | null;
}) {
  const input = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    if (focusRequest > 0) {
      input.current?.focus();
      input.current?.scrollIntoView({ block: 'nearest' });
    }
  }, [focusRequest]);
  const reason = blockedReason ?? (!prompt.trim() ? 'Enter a prompt to send.' : null);
  return (
    <form
      data-testid="now-composer"
      onSubmit={(e) => {
        e.preventDefault();
        if (!reason) onSend();
      }}
    >
      <label htmlFor="now-prompt">Ask agent</label>
      <textarea
        id="now-prompt"
        ref={input}
        rows={3}
        value={prompt}
        onChange={(e) => onChange(e.target.value)}
      />
      <span className="control-with-hint">
        <button
          type="submit"
          disabled={reason !== null}
          title={reason ?? 'Send prompt'}
          aria-describedby={reason ? 'now-send-reason' : undefined}
        >
          Send
        </button>
        {reason && (
          <span id="now-send-reason" className="row-meta control-hint" data-testid="now-send-reason">
            {reason}
          </span>
        )}
      </span>
    </form>
  );
}
