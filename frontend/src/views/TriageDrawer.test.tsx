import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import TriageDrawer from './TriageDrawer';
import { EMPTY_BADGE_COUNTS } from '../tabBadges';

const base = {
  open: true,
  section: 'review' as const,
  onSectionChange: () => {},
  onClose: () => {},
  repo: 'remote-server',
  onAskAgent: () => {},
  onOpenChat: () => {},
  onOpenHistory: () => {},
  badgeCounts: EMPTY_BADGE_COUNTS,
};

describe('TriageDrawer (D5)', () => {
  it('closed drawer renders nothing', () => {
    const html = renderToStaticMarkup(<TriageDrawer {...base} open={false} />);
    expect(html).not.toContain('data-testid="triage-drawer"');
  });

  it('renders the section switcher with Briefing included (two-tap guard)', () => {
    const html = renderToStaticMarkup(<TriageDrawer {...base} />);
    for (const s of ['review', 'jobs', 'queue', 'briefing']) {
      expect(html).toContain(`data-testid="triage-section-${s}"`);
    }
  });

  it('hides the Briefing section on the rollup picker (per-repo view)', () => {
    const html = renderToStaticMarkup(<TriageDrawer {...base} repo="all" />);
    expect(html).not.toContain('data-testid="triage-section-briefing"');
    expect(html).toContain('data-testid="triage-section-review"');
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

  it('marks exactly the held section pressed', () => {
    const html = renderToStaticMarkup(<TriageDrawer {...base} section="jobs" />);
    expect(html).toMatch(/data-testid="triage-section-jobs"[^>]*aria-pressed="true"/);
    expect(html).not.toMatch(/data-testid="triage-section-review"[^>]*aria-pressed="true"/);
  });
});
