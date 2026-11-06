// fallow-ignore-file unused-file
// Playwright loads this by glob/config, not by import — unreachable in fallow's graph by design.
import { expect, test } from '@playwright/test';

// Job trigger→green run: the Doctor dispatch (a td allowlist
// command needing no PR) runs through the real engine against the
// stub terminal and lands green. Dispatched via the command palette
// (#1016 demoted the Jobs whole-repo buttons).
test.describe('jobs run', () => {
  test('td doctor dispatch finishes green', async ({ page }) => {
    await page.goto('/');
    await page.getByTestId('palette-open').click();
    await page.getByTestId('palette-input').fill('doctor');
    await page.getByTestId('palette-item-run-doctor').click();
    await expect(page.getByTestId('jobs-view')).toBeVisible();

    // Palette dispatch is fire-and-observe: the Jobs-view-local
    // launch notice never fires, so assert on the runs list itself.
    // The stubbed run finishes in ~1s; toContainText polls.
    await expect(page.getByTestId('jobs-list')).toContainText(/doctor/i, {
      timeout: 30_000,
    });
    await expect(page.getByTestId('jobs-list')).toContainText('exit 0', {
      timeout: 30_000,
    });
  });
});
