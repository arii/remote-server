// fallow-ignore-file unused-file
// Playwright loads this by glob/config, not by import — unreachable in fallow's graph by design.
import { expect, test } from '@playwright/test';

test.describe('chat-first home', () => {
  test('opens on the conversation with the status strip above the composer', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByTestId('chat-view')).toBeVisible();
    await expect(page.getByTestId('nav-chat')).toHaveAttribute('aria-pressed', 'true');
    await expect(page.getByTestId('status-strip')).toBeVisible();
    // Strip sits above the composer in the chat column.
    const stripY = (await page.getByTestId('status-strip').boundingBox())!.y;
    const composerY = (await page.getByTestId('chat-prompt').boundingBox())!.y;
    expect(stripY).toBeLessThan(composerY);
  });

  test('strip tap opens Review; the top bar reaches Briefing without a drawer switcher', async ({ page }) => {
    await page.goto('/');
    await page.getByTestId('status-strip').click();
    const drawer = page.getByTestId('triage-drawer');
    await expect(drawer).toBeVisible();
    await expect(drawer.getByTestId('review-view')).toBeVisible();
    await expect(drawer.locator('[data-testid^="triage-section-"]')).toHaveCount(0);
    await drawer.getByTestId('triage-close').click();
    await page.getByTestId('nav-briefing').click();
    await expect(page.getByTestId('triage-drawer').getByTestId('briefing-view')).toBeVisible();
  });

  test('the top bar opens every triage destination directly', async ({ page }) => {
    await page.goto('/');
    const views = page.getByRole('navigation', { name: 'Views' });
    await expect(views.getByTestId('nav-chat')).toBeVisible();
    await expect(views.getByTestId('nav-now')).toBeVisible();
    for (const section of ['review', 'jobs', 'queue', 'briefing']) {
      await views.getByTestId(`nav-${section}`).click();
      await expect(page.getByTestId('triage-drawer').getByTestId(`${section}-view`)).toBeVisible();
      await expect(page.getByTestId('now-view')).toHaveCount(0);
      await expect(page.getByTestId('chat-view')).toHaveCount(0);
      await expect(views.locator('[aria-current="page"]')).toHaveCount(1);
      await expect(views.getByTestId(`nav-${section}`)).toHaveAttribute('aria-current', 'page');
      await page.getByTestId('triage-close').click();
      await expect(page.getByTestId('now-view')).toBeVisible();
      await expect(views.getByTestId('nav-now')).toHaveAttribute('aria-current', 'page');
    }
  });
});
