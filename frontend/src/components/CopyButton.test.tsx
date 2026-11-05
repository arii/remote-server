import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import CopyButton from './CopyButton';

describe('copy button (issue #907)', () => {
  it('renders idle with the copy label', () => {
    const html = renderToStaticMarkup(
      <CopyButton text="some blob" testid="copy-thing" />,
    );
    expect(html).toContain('data-testid="copy-thing"');
    expect(html).toContain('>Copy</button>');
  });

  it('honors a custom label', () => {
    const html = renderToStaticMarkup(
      <CopyButton text="a/b.py" testid="copy-path" label="Copy path" />,
    );
    expect(html).toContain('Copy path');
  });
});
