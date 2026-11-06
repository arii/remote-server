// fallow-ignore-file unused-file
// Playwright loads this by glob/config, not by import — unreachable in fallow's graph by design.
import { expect, test } from '@playwright/test';

// Co-browse / topbar responsive layout (operator UX feedback):
// the chat rail must hold 300-380px without mid-word clipping at
// desktop widths, collapse below the browser under 1100px, and the
// top bar must read as one row: context, views, tools in order.
// Stateless: opens the drawer and measures, mutates nothing.
test.describe('responsive console layout', () => {
  for (const width of [1280, 1440, 1920]) {
    test(`drawer rail holds 300-380px at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await page.goto('/');
      await page.getByTestId('tool-browser').click();
      await expect(page.getByTestId('cobrowse-drawer')).toBeVisible();

      const rail = page.getByTestId('cobrowse-drawer').locator('.cobrowse-chat');
      const box = await rail.boundingBox();
      expect(box).not.toBeNull();
      expect(box!.width).toBeGreaterThanOrEqual(300);
      expect(box!.width).toBeLessThanOrEqual(380);

      // Prompt chips must not clip mid-word: no horizontal
      // overflow on any rail button.
      const overflows = await page
        .getByTestId('chat-prompt-chips')
        .locator('button')
        .evaluateAll((btns) =>
          btns.map((b) => b.scrollWidth - b.clientWidth),
        );
      expect(overflows.length).toBeGreaterThan(0);
      for (const extra of overflows) expect(extra).toBeLessThanOrEqual(1);

      // The frame area names its state: connecting overlay or a
      // loaded frame — never a bare black box with no affordance.
      const status = page.getByTestId('cobrowse-frame-status');
      const frame = page.getByTestId('cobrowse-frame');
      await expect(status.or(frame)).toBeVisible({ timeout: 15_000 });
    });
  }

  test('top bar reads as one row with views between context and tools', async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/');
    const bar = page.getByTestId('topbar');
    await expect(bar).toBeVisible();
    const rows = await bar
      .locator(':scope > div, :scope > nav')
      .evaluateAll((els) =>
        els.map((e) => {
          const r = e.getBoundingClientRect();
          return { top: r.top, bottom: r.bottom };
        }),
      );
    // One visual row: every direct group's vertical band overlaps
    // the first group's band (equal tops is too strict — group
    // padding differs, shared row is what the eye reads).
    for (const r of rows.slice(1)) {
      expect(r.top).toBeLessThan(rows[0].bottom);
      expect(r.bottom).toBeGreaterThan(rows[0].top);
    }
    // Order left to right: repo context, then view tabs, then
    // tool links (viewport x-edges; offsetTop is meaningless
    // across different offset parents).
    const xs = await bar.evaluate((el) => {
      const left = (sel: string) =>
        (el.querySelector(sel) as HTMLElement).getBoundingClientRect().left;
      return [left('.topbar-group'), left('nav'), left('[data-testid="palette-open"]')];
    });
    expect(xs[1]).toBeGreaterThan(xs[0]);
    expect(xs[2]).toBeGreaterThan(xs[1]);
  });

  test('drawer stacks chat below the browser under 1100px', async ({ page }) => {
    await page.setViewportSize({ width: 1000, height: 900 });
    await page.goto('/');
    await page.getByTestId('tool-browser').click();
    await expect(page.getByTestId('cobrowse-drawer')).toBeVisible();
    const [frameTop, chatTop] = await page.getByTestId('cobrowse-drawer').evaluate((el) => {
      const f = (el.querySelector('.cobrowse-frame-wrap') as HTMLElement).getBoundingClientRect().top;
      const c = (el.querySelector('.cobrowse-chat') as HTMLElement).getBoundingClientRect().top;
      return [f, c];
    });
    // Single column: chat starts below the frame, not beside it.
    expect(chatTop).toBeGreaterThan(frameTop);
  });
});
