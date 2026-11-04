import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import BranchField, { useBranchField } from './BranchField';

function Probe({ repo, def }: { repo: string; def?: string }) {
  const [branch] = useBranchField(repo, def);
  return <span data-testid="probe">{branch}</span>;
}

describe('branch field (issue #708)', () => {
  it('renders the label and current value', () => {
    const html = renderToStaticMarkup(
      <BranchField id="b" testid="b-field" value="main" onChange={() => {}} />,
    );
    expect(html).toContain('data-testid="b-field"');
    expect(html).toContain('value="main"');
    expect(html).toContain('Branch');
  });

  it('seeds state from the repo default, empty without one', () => {
    const seeded = renderToStaticMarkup(<Probe repo="r" def="main" />);
    expect(seeded).toContain('>main</span>');
    const bare = renderToStaticMarkup(<Probe repo="r" />);
    expect(bare).toContain('data-testid="probe"></span>');
  });
});
