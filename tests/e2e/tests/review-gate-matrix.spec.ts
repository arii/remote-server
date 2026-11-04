import { expect, test } from '@playwright/test';

// Approve→merge gate matrix + the #848 self-approval regression lock.
//
// Seed rows (fixtures/state/seed.json):
// - PR 101: own (author == gh viewer), green, no required reviews.
// - PR 102: foreign, REVIEW_REQUIRED, green.
// - PR 103: foreign, approved, failing CI (read-only here; the
//   merge-block spec owns it).
test.describe('review approve/merge gate matrix', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await page.getByTestId('inbox-open-review').click();
    await expect(page.getByTestId('review-pr-list')).toBeVisible();
  });

  test('own PR: approve disabled with inline reason, merge enabled', async ({ page }) => {
    const row = page.getByTestId('review-pr-101');
    await expect(row).toBeVisible();
    // No required-review rule in play: merge-clean, not blocked.
    await expect(row).toContainText('merge-clean');
    await expect(page.getByTestId('approve-pr-101')).toBeDisabled();
    await expect(page.getByTestId('approve-self-note-101')).toContainText(
      'GitHub forbids self-approval',
    );
    await expect(page.getByTestId('merge-pr-101')).toBeEnabled();
  });

  test('foreign unapproved PR: merge blocked, approve live', async ({ page }) => {
    const row = page.getByTestId('review-pr-102');
    await expect(row).toContainText('Blocked: review is REVIEW_REQUIRED');
    await expect(page.getByTestId('approve-pr-102')).toBeEnabled();
    await expect(page.getByTestId('merge-pr-102')).toBeDisabled();
  });

  test('#848 lock: approving your own PR is a readable 400, never a 500', async ({
    page,
  }) => {
    const resp = await page.request.post('/api/pr-action', {
      data: {
        repo: 'remote-server',
        pr_number: 101,
        action: 'approve',
        intent: 'pr-approve',
        confirmed: true,
      },
    });
    expect(resp.status()).toBe(400);
    const body = await resp.json();
    expect(String(body.detail)).toContain('forbids approving your own');
  });

  test('own green PR merges in one tap', async ({ page }) => {
    // The branch field is optional: blank keeps the run
    // checkout-agnostic (CI checks out a detached HEAD).
    await page.getByTestId('review-branch').fill('');
    await page.getByTestId('merge-pr-101').click();
    await page.getByRole('button', { name: 'Confirm Merge' }).click();
    await expect(page.getByTestId('review-notice')).toContainText('Merged PR #101.');
    await expect(page.getByTestId('review-pr-101')).toBeHidden();
  });

  test('foreign PR approves then merges', async ({ page }) => {
    await page.getByTestId('review-branch').fill('');
    await page.getByTestId('approve-pr-102').click();
    await expect(page.getByTestId('review-notice')).toContainText('Approved PR #102.');
    // The list re-reads after the review: the row flips merge-clean.
    await expect(page.getByTestId('review-pr-102')).toContainText('merge-clean');
    await page.getByTestId('merge-pr-102').click();
    await page.getByRole('button', { name: 'Confirm Merge' }).click();
    await expect(page.getByTestId('review-notice')).toContainText('Merged PR #102.');
    await expect(page.getByTestId('review-pr-102')).toBeHidden();
    // The untouched failing PR is still triageable.
    await expect(page.getByTestId('review-pr-103')).toBeVisible();
  });
});
