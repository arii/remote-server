import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { isStalled, STALL_SECONDS, StreamProgress, streamProgress } from './ChatView';

describe('stream progress (issue #376)', () => {
  it('formats elapsed alone before anything arrives', () => {
    expect(streamProgress(0, null, 0)).toBe('Working… 0s');
    expect(streamProgress(45, null, 0)).toBe('Working… 45s');
  });

  it('appends phase and token count', () => {
    expect(streamProgress(45, 'prefill', 0)).toBe('Working… 45s · prefill');
    expect(streamProgress(45, 'prefill', 12)).toBe('Working… 45s · prefill · 12 tok');
    expect(streamProgress(45, null, 12)).toBe('Working… 45s · 12 tok');
  });

  it('renders the progress line', () => {
    const html = renderToStaticMarkup(
      <StreamProgress elapsed={45} phase="prefill" tokens={12} />,
    );
    expect(html).toContain('data-testid="chat-progress"');
    expect(html).toContain('Working… 45s · prefill · 12 tok');
  });
});

describe('stall watchdog (issue #376)', () => {
  it('flags streams silent past the stall window', () => {
    expect(STALL_SECONDS).toBe(180);
    expect(isStalled(0, 179_999)).toBe(false);
    expect(isStalled(0, 180_000)).toBe(false);
    expect(isStalled(0, 180_001)).toBe(true);
  });
});
