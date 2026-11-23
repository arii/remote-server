// fallow-ignore-file unused-file
// Playwright loads this by glob/config, not by import — unreachable in fallow's graph by design.
import { expect, test, type Page } from '@playwright/test';

// Issue #1643: Ledger triage cards with long expanded content must stay usable
// on phone widths. Real layout is measured here because happy-dom computes none.
const longUrl = `https://example.com/${'segment'.repeat(60)}?q=${'x'.repeat(120)}`;
const problem = [
  `Unbroken string: ${'A'.repeat(300)}`,
  `Link: ${longUrl}`,
  'Code block:',
  `    const value = await fetchSomething("${'long-argument-'.repeat(12)}");`,
  ...Array.from({ length: 12 }, (_, i) => `Line ${i + 1}: multiline body text that keeps going so expanded content is tall.`),
].join('\n');

const rows = [
  {
    kind: 'ledger-intake',
    canonical_intake_id: 'intake-narrow-1',
    status: 'NEEDS_INFO',
    title: `Long title ${'unbroken'.repeat(30)}`,
    problem,
    source_url: longUrl,
    missing_fields: ['scope'],
  },
  {
    kind: 'ledger-intake',
    canonical_intake_id: 'intake-narrow-2',
    status: 'SUSPECTED-DUPLICATE',
    title: 'Incoming duplicate',
    problem,
    duplicate_candidate: {
      intake_id: 'intake-old',
      canonical_intake_id: 'intake-old',
      title: `Candidate ${'wordless'.repeat(30)}`,
      status: 'READY',
    },
  },
];

async function overflowReport(page: Page) {
  return page.evaluate(() => {
    const vw = document.documentElement.clientWidth;
    const card = document.querySelector('.ledger-triage-card') as HTMLElement;
    const escaped: string[] = [];
    for (const el of Array.from(document.querySelectorAll('.ledger-triage-card *')) as HTMLElement[]) {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && (r.right > vw + 1 || r.left < -1)) escaped.push(`${el.tagName}.${el.className}`);
    }
    return {
      pageOverflow: document.documentElement.scrollWidth - vw,
      cardOverflow: card.scrollWidth - card.clientWidth,
      escaped,
    };
  });
}

for (const width of [320, 375, 430]) {
  test(`triage cards with long expanded content fit at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 800 });
    await page.route(/\/api\/ledger-intake$/, (route) => route.fulfill({ json: { rows } }));
    await page.goto('/');
    await page.getByTestId('nav-ledger').click();
    const cards = page.locator('.ledger-triage-card');
    await expect(cards).toHaveCount(2);
    for (const summary of await page.locator('.ledger-card-problem summary').all()) await summary.click();
    await expect(page.locator('.ledger-card-problem[open]')).toHaveCount(2);
    await page.screenshot({ path: testInfo.outputPath(`triage-${width}.png`), fullPage: true });

    const report = await overflowReport(page);
    expect(report.pageOverflow, 'page must not scroll sideways').toBeLessThanOrEqual(1);
    expect(report.cardOverflow, 'card content must not be clipped or overflow').toBeLessThanOrEqual(1);
    expect(report.escaped, 'no element may extend past the viewport').toEqual([]);

    // Status badge and primary action are reachable by scrolling and stay inside the viewport.
    const approve = cards.first().getByRole('button', { name: 'Approve and mark READY' });
    await approve.scrollIntoViewIfNeeded();
    const box = await approve.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(width + 1);
    await cards.first().locator('.status-badge').scrollIntoViewIfNeeded();
    await expect(cards.first().locator('.status-badge')).toBeInViewport();

    // Interactive controls inside each card must not overlap one another.
    const overlaps = await page.evaluate(() => {
      const found: string[] = [];
      for (const card of Array.from(document.querySelectorAll('.ledger-triage-card'))) {
        const controls = Array.from(card.querySelectorAll('button, input, summary, a')) as HTMLElement[];
        const rects = controls.map((el) => ({ el, r: el.getBoundingClientRect() })).filter(({ r }) => r.width > 0 && r.height > 0);
        for (let i = 0; i < rects.length; i += 1) {
          for (let j = i + 1; j < rects.length; j += 1) {
            const a = rects[i].r;
            const b = rects[j].r;
            if (rects[i].el.contains(rects[j].el) || rects[j].el.contains(rects[i].el)) continue;
            if (a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1) {
              found.push(`${rects[i].el.tagName} overlaps ${rects[j].el.tagName}`);
            }
          }
        }
      }
      return found;
    });
    expect(overlaps, 'controls must not overlap').toEqual([]);
  });
}
