// fallow-ignore-file unused-file
// Playwright loads this by glob/config, not by import — unreachable in fallow's graph by design.
import { expect, test } from '@playwright/test';

// Job trigger→green run: the Doctor dispatch (a td allowlist
// command needing no PR) runs through the real engine against the
// stub terminal and lands green.
test.describe('jobs run', () => {
  test('td doctor dispatch finishes green', async ({ page }) => {
    await page.goto('/');
    await page.getByTestId('inbox-open-jobs').click();
    await expect(page.getByTestId('jobs-view')).toBeVisible();

    // The branch field is optional: blank keeps the run
    // checkout-agnostic (CI checks out a detached HEAD).
    await page.getByTestId('jobs-branch').fill('');
    await page.getByTestId('dispatch-td-doctor').click();

    // Terminal state only: the stubbed run finishes in ~1s, so the
    // transient "Started …" notice is already gone when polled.
    await expect(page.getByTestId('jobs-notice')).toContainText(
      'Finished Doctor: all checks pass',
      { timeout: 30_000 },
    );
    await expect(page.getByTestId('jobs-result')).toContainText('all checks pass');
    await expect(page.getByTestId('jobs-error')).toBeHidden();
  });
});
