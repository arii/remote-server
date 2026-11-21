import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import ReviewView from './ReviewView';
import { parseIssueNumber } from './queueUtils';

describe('empty forms keep their submit disabled (#1491 findings 17 and 23)', () => {
  it('parses an issue number from the Queue box, null otherwise', () => {
    expect(parseIssueNumber('12')).toBe('12');
    expect(parseIssueNumber(' #345 ')).toBe('345');
    expect(parseIssueNumber('')).toBeNull();
    expect(parseIssueNumber('#')).toBeNull();
    expect(parseIssueNumber('12a')).toBeNull();
  });
  it('Review Create issue button is disabled with no title', () => {
    const html = renderToStaticMarkup(
      <ReviewView repo="remote-server" onAskAgent={() => {}} onSelectPr={() => {}} />,
    );
    const at = html.indexOf('Create issue');
    expect(at).toBeGreaterThan(-1);
    const tag = html.slice(html.lastIndexOf('<button', at), at);
    expect(tag).toContain('disabled');
  });
});
