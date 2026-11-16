import { useEffect, useRef } from 'react';

// Now hands Send to Chat; shortcuts only stage editable text (#1317).
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
      <button type="submit" disabled={reason !== null}>Send</button>
      {reason && <span className="row-meta">{reason}</span>}
    </form>
  );
}
