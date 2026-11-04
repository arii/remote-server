// fallow-ignore-file unused-file
// Playwright loads this by glob/config, not by import — unreachable in fallow's graph by design.
import { expect, test } from '@playwright/test';

// Agent launch→turn→abort through the drawer, against the mock
// provider seam (the stub `docker` answers `muse exec` with canned
// text — no model, no daemon, no spend).
test.describe('agent turn', () => {
  test('launch streams the mock answer into the transcript', async ({ page }) => {
    await page.goto('/');
    await page
      .getByRole('navigation', { name: 'Views' })
      .getByRole('button', { name: 'Chat', exact: true })
      .click();
    const drawer = page.getByTestId('agent-drawer');
    await expect(drawer).toBeVisible();

    // Both stubbed providers report present.
    await expect(page.getByTestId('agent-provider-dead')).toBeHidden();
    await drawer.getByTestId('agent-provider').selectOption('muse');
    await drawer.getByTestId('agent-prompt').fill('e2e smoke ping');
    await drawer.getByRole('button', { name: 'Launch' }).click();

    const answer = drawer.getByTestId('agent-msg-assistant-1');
    await expect(answer).toContainText('E2E mock turn answer');
    await expect(answer).toContainText('Second line of mock answer');
    await expect(drawer.getByTestId('agent-error')).toBeHidden();
  });

  test('a slow follow-up turn aborts cleanly', async ({ page }) => {
    await page.goto('/');
    await page
      .getByRole('navigation', { name: 'Views' })
      .getByRole('button', { name: 'Chat', exact: true })
      .click();
    const drawer = page.getByTestId('agent-drawer');
    await expect(drawer).toBeVisible();
    await drawer.getByTestId('agent-provider').selectOption('muse');

    // First turn completes so the follow-up runs on a server-known
    // session (the continued-turn path, with a real session id the
    // abort call targets).
    await drawer.getByTestId('agent-prompt').fill('e2e first turn');
    await drawer.getByRole('button', { name: 'Launch' }).click();
    await expect(drawer.getByTestId('agent-msg-assistant-1')).toContainText(
      'E2E mock turn answer',
    );

    await drawer.getByTestId('agent-followup').fill('SLOWTURN second turn');
    await drawer.getByTestId('agent-send').click();
    const abort = drawer.getByTestId('agent-abort');
    await expect(abort).toBeVisible();
    await abort.click();
    await expect(abort).toBeHidden();
    await expect(drawer.getByTestId('agent-error')).toBeHidden();
  });
});
