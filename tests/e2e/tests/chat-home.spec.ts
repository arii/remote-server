// fallow-ignore-file unused-file
// Playwright loads this by glob/config, not by import — unreachable in fallow's graph by design.
import { expect, test } from '@playwright/test';

// Chat-first shell (D5): the console opens on the conversation, the
// StatusStrip rides above the composer, and its tap opens the triage
// drawer where Review/Jobs/Queue — and the folded-down Briefing —
// live. Two-tap guard: Briefing's digest + notification controls
// must stay reachable from a cold open in two taps.
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

  test('strip tap opens the triage drawer; Briefing reachable in two taps', async ({ page }) => {
    await page.goto('/');
    await page.getByTestId('status-strip').click();
    const drawer = page.getByTestId('triage-drawer');
    await expect(drawer).toBeVisible();
    await expect(drawer.getByTestId('triage-section-review')).toBeVisible();
    await expect(drawer.getByTestId('triage-section-jobs')).toBeVisible();
    await expect(drawer.getByTestId('triage-section-queue')).toBeVisible();
    // Tap 1 was the strip; tap 2 lands the Briefing section with the
    // digest + notification controls + next actions.
    await drawer.getByTestId('triage-section-briefing').click();
    await expect(drawer.getByTestId('briefing-view')).toBeVisible();
  });

  test('Briefing is no longer a top-bar tab', async ({ page }) => {
    await page.goto('/');
    const views = page.getByRole('navigation', { name: 'Views' });
    await expect(views.getByTestId('nav-chat')).toBeVisible();
    await expect(views.getByTestId('nav-now')).toBeVisible();
    await expect(views.getByTestId('nav-briefing')).toHaveCount(0);
  });
});
