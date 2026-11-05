import { useState } from 'react';

// One-tap copy for raw blobs and paths (#907): clipboard API with
// a textarea fallback for non-secure contexts, honest label states.
export default function CopyButton({
  text,
  testid,
  label = 'Copy',
}: {
  text: string;
  testid: string;
  label?: string;
}) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle');

  async function onCopy() {
    try {
      if (
        typeof navigator !== 'undefined' &&
        navigator.clipboard?.writeText
      ) {
        await navigator.clipboard.writeText(text);
      } else if (typeof document !== 'undefined') {
        const area = document.createElement('textarea');
        area.value = text;
        document.body.appendChild(area);
        area.select();
        document.execCommand('copy');
        area.remove();
      } else {
        throw new Error('no clipboard');
      }
      setState('copied');
    } catch {
      setState('failed');
    }
  }

  return (
    <button
      type="button"
      className="btn-secondary"
      data-testid={testid}
      onClick={() => void onCopy()}
    >
      {state === 'copied' ? 'Copied' : state === 'failed' ? 'Copy failed' : label}
    </button>
  );
}
