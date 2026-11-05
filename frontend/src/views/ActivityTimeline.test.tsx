import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import ActivityTimeline, { TIMELINE_POLL_MS, TimelineList } from './ActivityTimeline';
import { toTimelineItems } from './timelineUtils';

describe('activity timeline (issue #812)', () => {
  it('renders the section head, refresh action, and loading state', () => {
    const html = renderToStaticMarkup(<ActivityTimeline />);
    expect(html).toContain('data-testid="timeline-view"');
    expect(html).toContain('Runs, turns, pulls, and merges, newest first.');
    expect(html).toContain('data-testid="timeline-refresh"');
    expect(html).toContain('data-testid="timeline-loading"');
  });

  it('renders interleaved rows newest first with per-feed markers', () => {
    const items = toTimelineItems(
      [{ id: 'r1', title: 'briefing', status: 'done', done: true, at: 100 }],
      [{
        id: 't1', script: 'muse', model: 'qwen3:8b', provider: 'ollama', thread: 'th1',
        repo: 'arii/boomtick',
        started_ts: 300, duration_s: 12, ok: true, tools: [], tokens: 1500,
        preview: 'did the thing', has_prompt: true, cost_usd: null, metered: false,
      }],
      [{ kind: 'webhook', event: 'webhook pull ok', at: 200, ok: true }],
      [{ kind: 'pr-action', event: 'merge: PR #5 merged', ts: 400 }],
    );
    const html = renderToStaticMarkup(<TimelineList items={items} />);
    expect(html).toContain('data-testid="timeline-list"');
    const order = ['merge', 'turn', 'pull', 'run'].map((f) => html.indexOf(`timeline-item-${f}`));
    expect(order.every((i) => i >= 0)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order);
    expect(html).toContain('merge ·');
    expect(html).toContain('turn ·');
  });

  it('explains an empty timeline', () => {
    const html = renderToStaticMarkup(<TimelineList items={[]} />);
    expect(html).toContain('data-testid="timeline-empty"');
    expect(html).toContain('No activity yet.');
  });

  it('polls on a bounded cadence', () => {
    expect(TIMELINE_POLL_MS).toBeGreaterThan(0);
    expect(TIMELINE_POLL_MS).toBeLessThanOrEqual(60_000);
  });
});
