import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import {
  reconcileSnoozed,
  RepoTag,
  SnoozedStub,
  SNOOZE_PRESETS,
  SnoozeButtons,
  snoozedRows,
  snoozePresetLabel,
} from './NowView';
import { ALL_REPOS, isRollupRepo } from '../components/RepoPicker';

describe('snooze presets (issue #614)', () => {
  it('offers 1h and 8h', () => {
    expect(SNOOZE_PRESETS).toEqual([60, 480]);
    expect(snoozePresetLabel(60)).toBe('Snooze 1h');
    expect(snoozePresetLabel(480)).toBe('Snooze 8h');
  });
});

describe('reconcileSnoozed (issue #614)', () => {
  const entry = { until: 2000, title: 't' };

  it('keeps live stubs missing from the list', () => {
    expect(reconcileSnoozed({ a: entry }, [], 1000)).toEqual({ a: entry });
  });

  it('drops expired stubs so items resurface', () => {
    expect(reconcileSnoozed({ a: entry }, [], 2000)).toEqual({});
    expect(reconcileSnoozed({ a: entry }, [], 3000)).toEqual({});
  });

  it('drops stubs whose item is back in the list', () => {
    expect(reconcileSnoozed({ a: entry }, ['a'], 1000)).toEqual({});
  });
});

describe('snoozedRows (issue #614)', () => {
  it('returns only stubs missing from the live list', () => {
    const snoozed = {
      a: { until: 9, title: 'A' },
      b: { until: 9, title: 'B' },
    };
    expect(snoozedRows(snoozed, ['b', 'c'])).toEqual([['a', snoozed.a]]);
  });
});

describe('SnoozeButtons (issue #614)', () => {
  it('renders one button per preset with stable testids', () => {
    const html = renderToStaticMarkup(
      <SnoozeButtons itemId="job-j1" busy={false} onSnooze={() => {}} />,
    );
    expect(html).toContain('data-testid="snooze-job-j1-60"');
    expect(html).toContain('data-testid="snooze-job-j1-480"');
    expect(html).toContain('Snooze 1h');
    expect(html).toContain('Snooze 8h');
    expect(html).not.toContain('disabled');
  });

  it('disables while a snooze is in flight', () => {
    const html = renderToStaticMarkup(
      <SnoozeButtons itemId="job-j1" busy onSnooze={() => {}} />,
    );
    expect(html).toContain('disabled');
  });
});

describe('SnoozedStub (issue #614)', () => {
  it('collapses the card with an undo affordance', () => {
    const html = renderToStaticMarkup(
      <SnoozedStub itemId="job-j1" title="Failed job" until={1793760000} busy={false} onUnsnooze={() => {}} />,
    );
    expect(html).toContain('data-testid="now-snoozed-job-j1"');
    expect(html).toContain('Snoozed: Failed job');
    expect(html).toContain('Hidden until');
    expect(html).toContain('data-testid="unsnooze-job-j1"');
    expect(html).toContain('Unsnooze');
    expect(html).not.toContain('disabled');
  });

  it('disables undo while unsnoozing', () => {
    const html = renderToStaticMarkup(
      <SnoozedStub itemId="job-j1" title="Failed job" until={1793760000} busy onUnsnooze={() => {}} />,
    );
    expect(html).toContain('disabled');
    expect(html).toContain('Unsnoozing…');
  });
});

describe('rollup repo (issue #610)', () => {
  it('recognizes only the All repos pseudo-repo', () => {
    expect(ALL_REPOS).toBe('all');
    expect(isRollupRepo('all')).toBe(true);
    expect(isRollupRepo('remote-server')).toBe(false);
    expect(isRollupRepo('')).toBe(false);
  });

  it('RepoTag renders a stable per-repo chip', () => {
    const html = renderToStaticMarkup(<RepoTag repo="boomtick" />);
    expect(html).toContain('data-testid="now-repo-boomtick"');
    expect(html).toContain('repo-chip');
    expect(html).toContain('boomtick');
  });
});
