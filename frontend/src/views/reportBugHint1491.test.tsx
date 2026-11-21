import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { ReportDialog } from './NowView';

describe('File bug says why it is disabled (#1491 finding 26)', () => {
  it('shows a visible hint while the title is empty', () => {
    const html = renderToStaticMarkup(
      <ReportDialog initialBody="" onSubmit={() => {}} onCancel={() => {}} />,
    );
    expect(html).toContain('data-testid="report-submit-hint"');
    expect(html).toContain('Enter a title to enable File bug.');
  });
});
