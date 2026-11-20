import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import TriageDrawer from './TriageDrawer';

const base = {
  open: true,
  section: 'review' as const,
  onSectionChange: () => {},
  onClose: () => {},
  repo: 'remote-server',
  onAskAgent: () => {},
  onOpenChat: () => {},
  onOpenHistory: () => {},
};

describe('TriageDrawer (D5)', () => {
  it('closed drawer renders nothing', () => {
    const html = renderToStaticMarkup(<TriageDrawer {...base} open={false} />);
    expect(html).not.toContain('data-testid="triage-drawer"');
  });

  it('leaves section navigation to the top header (#1317)', () => {
    const html = renderToStaticMarkup(<TriageDrawer {...base} />);
    for (const s of ['review', 'jobs', 'queue', 'briefing']) {
      expect(html).not.toContain(`data-testid="triage-section-${s}"`);
    }
  });


  it('mounts the requested section view', () => {
    expect(renderToStaticMarkup(<TriageDrawer {...base} />)).toContain(
      'data-testid="review-view"',
    );
    // Static render catches Briefing pre-fetch (its effect has not
    // run), so assert its loading state, not the data view.
    expect(
      renderToStaticMarkup(
        <TriageDrawer {...base} section="briefing" />,
      ),
    ).toContain('data-testid="briefing-loading"');
  });

  it('renders RollupGate for review, jobs, and briefing when repo is all (#1386)', () => {
    for (const s of ['review', 'jobs', 'briefing'] as const) {
      const html = renderToStaticMarkup(
        <TriageDrawer {...base} repo="all" section={s} />,
      );
      expect(html).toContain('data-testid="rollup-gate"');
      expect(html).toContain('needs one repo');
    }
  });

  it('keeps the close control when showing a section', () => {
    const html = renderToStaticMarkup(<TriageDrawer {...base} section="jobs" />);
    expect(html).toContain('data-testid="triage-close"');
  });
});
