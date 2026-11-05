// fallow-ignore-file unused-file
// Playwright loads this by glob/config, not by import — unreachable in fallow's graph by design.
import { expect, test } from '@playwright/test';

// Review pane decision context: a blocked PR names its block
// inputs inline (head reason + failing checks).
test.describe('review merge-block context', () => {
  test('details pane shows the merge-block reason and failing checks', async ({
    page,
  }) => {
    await page.goto('/');
    await page.getByTestId('nav-review').click();
    await expect(page.getByTestId('review-pr-list')).toBeVisible();

    // Issue #932 renamed the row opener to an explicit Details
    // button (Open on GitHub now lives inside the details pane).
    await page.getByTestId('details-pr-103').click();
    await expect(page.getByTestId('review-details')).toBeVisible();
    await expect(page.getByTestId('review-merge-block')).toContainText(
      'Not mergeable: CI failing, merge blocked.',
    );
    await expect(page.getByTestId('review-merge-failing')).toContainText(
      'Failing checks: e2e-ci.',
    );
  });
});
