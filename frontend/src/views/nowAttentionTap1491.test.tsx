// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from 'vitest';
import { revealAttention } from './NowView';

afterEach(() => {
  document.body.innerHTML = '';
});

it('tapping the hero attention count scrolls to and focuses the Attention section (#1491 finding 3)', () => {
  document.body.innerHTML = '<section data-testid="now-attention" tabindex="-1"><h2>Attention</h2></section>';
  const el = document.querySelector<HTMLElement>('[data-testid="now-attention"]')!;
  const scroll = vi.fn();
  el.scrollIntoView = scroll;
  expect(revealAttention(document)).toBe(true);
  expect(scroll).toHaveBeenCalledTimes(1);
  expect(document.activeElement).toBe(el);
});

it('reports false when the section is not rendered', () => {
  expect(revealAttention(document)).toBe(false);
});
