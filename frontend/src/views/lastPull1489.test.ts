import { describe, expect, it } from 'vitest';
import { lastPullLines } from './historyUtils';

const NOW = 1_000_000;

describe('last pull per repo lines (#1489)', () => {
  it('reads an advancing pull as ok, a skip as muted, a failure as bad with detail', () => {
    const lines = lastPullLines(
      {
        beta: { at: NOW - 600, ok: false, advanced: false, detail: 'fetch failed: could not resolve host' },
        alpha: { at: NOW - 30, ok: true, advanced: true, detail: 'fast-forwarded main (3 commits)' },
        gamma: { at: NOW - 7200, ok: true, advanced: false, detail: 'skipped: tree dirty', skip_reason: 'tree dirty' },
      },
      NOW,
    );
    expect(lines.map((l) => l.repo)).toEqual(['alpha', 'beta', 'gamma']);
    expect(lines[0]).toMatchObject({ tone: 'ok' });
    expect(lines[0].text).toContain('pulled new commits just now');
    expect(lines[1]).toMatchObject({ tone: 'bad' });
    expect(lines[1].text).toContain('pull failed 10 min ago: fetch failed: could not resolve host');
    expect(lines[2]).toMatchObject({ tone: 'muted' });
    expect(lines[2].text).toContain('checkout not updated');
    expect(lines[2].text).toContain('2 h ago');
  });
  it('does not call a failure with no detail ok and handles a missing time', () => {
    const [l] = lastPullLines({ r: { at: null, ok: false, advanced: false, detail: '' } }, NOW);
    expect(l.tone).toBe('bad');
    expect(l.text).toContain('time unknown');
    expect(l.text).toContain('no detail recorded');
  });
  it('is empty when no pull happened since the backend started', () => {
    expect(lastPullLines({}, NOW)).toEqual([]);
  });
});
