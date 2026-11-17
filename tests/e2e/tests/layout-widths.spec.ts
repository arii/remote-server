// fallow-ignore-file unused-file
// Playwright loads this by glob/config, not by import — unreachable in fallow's graph by design.
import { expect, test } from '@playwright/test';

// Co-browse / topbar responsive layout (operator UX feedback):
// the agent rail must hold 300-380px without mid-word clipping at
// desktop widths, stack below the browser under 1100px, and the
// top bar must expose all destinations without horizontal scrolling.
// Stateless: opens the Browser tab and measures, mutates nothing.
test.describe('responsive console layout', () => {
  for (const width of [1280, 1440, 1920]) {
    test(`agent rail holds 300-380px at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await page.goto('/');
      await page.getByTestId('nav-browser').click();
      await expect(page.getByTestId('keepalive-browser')).toBeVisible();

      const rail = page.getByTestId('cobrowse-drawer').locator('.cobrowse-chat');
      const box = await rail.boundingBox();
      expect(box).not.toBeNull();
      expect(box!.width).toBeGreaterThanOrEqual(300);
      expect(box!.width).toBeLessThanOrEqual(380);

      // Rail buttons must not clip mid-word: no horizontal
      // overflow on any agent-rail button.
      const overflows = await page
        .getByTestId('agent-drawer')
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

  for (const width of [1280, 1440]) {
    test(`all header destinations fit without scrolling at ${width}px`, async ({
      page,
    }, testInfo) => {
      await page.setViewportSize({ width, height: 900 });
      await page.goto('/');
      await expect(page.getByTestId('stack-status')).not.toContainText('loading');
      await expect(page.getByTestId('nav-badge-review')).toBeVisible();
      const bar = page.getByTestId('topbar');
      await expect(bar).toBeVisible();
      const controls = bar.locator('button, a, select');
      for (const control of await controls.all()) {
        await expect(control).toBeVisible();
        const box = (await control.boundingBox())!;
        expect(box.x).toBeGreaterThanOrEqual(0);
        expect(box.x + box.width).toBeLessThanOrEqual(width);
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1);
      const nav = bar.getByRole('navigation', { name: 'Views' });
      await expect(nav.locator('button')).toHaveCount(8);
      expect(await nav.evaluate((el) => el.scrollWidth - el.clientWidth)).toBeLessThanOrEqual(1);
      for (const overflow of await nav.locator('button').evaluateAll((buttons) =>
        buttons.map((button) => button.scrollWidth - button.clientWidth),
      )) {
        expect(overflow).toBeLessThanOrEqual(1);
      }
      await page.screenshot({ path: testInfo.outputPath(`header-${width}.png`) });
    });
  }

  test('all header destinations wrap without clipping at 360px', async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 360, height: 844 });
    await page.goto('/');
    await expect(page.getByTestId('stack-status')).not.toContainText('loading');
    await expect(page.getByTestId('nav-badge-review')).toBeVisible();
    const bar = page.getByTestId('topbar');
    const nav = bar.getByRole('navigation', { name: 'Views' });
    await expect(nav.locator('button')).toHaveCount(8);
    const rows = await nav.locator('button').evaluateAll((buttons) =>
      buttons.map((button) => Math.round(button.getBoundingClientRect().top)),
    );
    expect(new Set(rows).size).toBeGreaterThan(1);
    const controls = await bar.locator('button, a, select').evaluateAll((elements) =>
      elements.map((element) => {
        const rect = element.getBoundingClientRect();
        return { left: rect.left, right: rect.right };
      }),
    );
    for (const control of controls) {
      expect(control.left).toBeGreaterThanOrEqual(0);
      expect(control.right).toBeLessThanOrEqual(360);
    }
    await page.screenshot({ path: testInfo.outputPath('header-360.png') });
  });

  test('header and page never overflow at 390px across views', async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/');
    await expect(page.getByTestId('topbar')).toBeVisible();
    for (const t of ['repo-picker', 'stack-status', 'palette-open', 'tool-terminal', 'tool-code']) {
      const box = await page.getByTestId(t).boundingBox();
      expect(box).not.toBeNull();
      expect(box!.x).toBeGreaterThanOrEqual(0);
      expect(box!.x + box!.width).toBeLessThanOrEqual(390);
    }
    for (const view of ['chat', 'now', 'history', 'browser', 'review', 'jobs', 'queue', 'briefing']) {
      await page.getByTestId(`nav-${view}`).click();
      await expect(page.getByTestId(`nav-${view}`)).toHaveAttribute('aria-current', 'page');
      await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1);
    }
    await page.getByTestId('nav-jobs').click();
    await page.screenshot({ path: testInfo.outputPath('jobs-390.png') });
  });

  test('page stacks the agent rail below the browser under 1100px', async ({ page }) => {
    await page.setViewportSize({ width: 1000, height: 900 });
    await page.goto('/');
    await page.getByTestId('nav-browser').click();
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
