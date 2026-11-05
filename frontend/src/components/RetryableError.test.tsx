import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import RetryableError from './RetryableError';

describe('retryable error (issue #907)', () => {
  it('names the problem and offers retry inline', () => {
    const html = renderToStaticMarkup(
      <RetryableError
        testid="thing-error"
        message="Couldn't load things: boom"
        retryTestid="thing-retry"
        onRetry={() => {}}
      />,
    );
    expect(html).toContain('data-testid="thing-error"');
    expect(html).toContain('role="alert"');
    expect(html).toContain('boom');
    expect(html).toContain('data-testid="thing-retry"');
    expect(html).toContain('>Retry</button>');
  });
});
