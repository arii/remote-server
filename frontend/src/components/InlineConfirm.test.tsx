import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import InlineConfirm from './InlineConfirm';

describe('InlineConfirm', () => {
  it('renders initial button with label', () => {
    const html = renderToStaticMarkup(
      <InlineConfirm
        label="Merge approved PR"
        prompt="Merge approved PRs? This cannot be undone."
        onConfirm={() => {}}
        data-testid="danger-merge-btn"
      />,
    );
    expect(html).toContain('Merge approved PR');
    expect(html).toContain('data-testid="danger-merge-btn"');
    expect(html).not.toContain('Confirm');
    expect(html).not.toContain('Cancel');
  });

  it('renders disabled state when disabled is true', () => {
    const html = renderToStaticMarkup(
      <InlineConfirm
        label="Clear finished"
        disabled
        onConfirm={() => {}}
      />,
    );
    expect(html).toContain('disabled=""');
  });
});
