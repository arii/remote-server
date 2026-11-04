// Console UX audit runner for `td ux` (#880).
//
// Vendored from tech-dancer scripts/ux-audit-runner.ts (same td
// invocation contract, same output layout). Config loads through
// ./ux-config, which adds the UX_BASE_URL env override so scheduled
// fires can target the deployed console without editing the file.
// Invoked by td with cwd=checkout:
//   pnpm exec tsx scripts/ux-audit-runner.ts <route> [viewport]
// fallow-ignore-file unused-file
import { chromium } from 'playwright';
import { AxeBuilder } from '@axe-core/playwright';
import fs from 'fs';
import path from 'path';
import { loadUxConfig } from './ux-config';

interface AuditResult {
  route: string;
  viewport: string;
  timestamp: string;
  accessibility: unknown;
  overflow: unknown[];
  images: unknown[];
  tapTargets: unknown[];
  aboveTheFold: unknown;
  screenshot: string;
}

// Vendored audit sequence: kept line-faithful to upstream so future
// reference syncs stay clean (see file header).
// fallow-ignore-next-line complexity
async function runAudit(url: string, route: string, viewport: { name: string, width: number, height: number }, options: { imagesOnly?: boolean, overflowOnly?: boolean, contrastOnly?: boolean } = {}) {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: viewport.width, height: viewport.height }
  });
  const page = await context.newPage();

  const resultsDir = path.join(process.cwd(), 'artifacts', 'ux-audit', 'results');
  const screenshotsDir = path.join(process.cwd(), 'artifacts', 'ux-audit', 'screenshots');

  // Ensure directories exist
  if (!fs.existsSync(resultsDir)) fs.mkdirSync(resultsDir, { recursive: true });
  if (!fs.existsSync(screenshotsDir)) fs.mkdirSync(screenshotsDir, { recursive: true });

  const slug = route.replace(/\//g, '_').replace(/^_/, '') || 'home';
  const screenshotName = `${slug}-${viewport.name}.png`;
  const screenshotPath = path.join(screenshotsDir, screenshotName);

  try {
    console.log(`Auditing ${route} at ${viewport.width}x${viewport.height}...`);
    await page.goto(url, { waitUntil: 'networkidle' });

    let accessibility: unknown = null;
    let overflow: unknown[] = [];
    let images: unknown[] = [];
    let tapTargets: unknown[] = [];
    let aboveTheFold: unknown = null;

    const runAll = !options.imagesOnly && !options.overflowOnly && !options.contrastOnly;

    // 1. Accessibility & Contrast
    if (runAll || options.contrastOnly) {
      accessibility = await new AxeBuilder({ page }).analyze().catch((e: Error) => ({ error: e.message }));
    }

    // 2. Screenshots
    await page.screenshot({ path: screenshotPath, fullPage: true });

    // 3. Layout Overflow
    if (runAll || options.overflowOnly) {
      overflow = await page.evaluate(() => {
        const issues: unknown[] = [];
        const docWidth = document.documentElement.offsetWidth;
        document.querySelectorAll('*').forEach(el => {
          const rect = el.getBoundingClientRect();
          if (rect.right > docWidth || rect.left < 0) {
            issues.push({
              tagName: el.tagName,
              id: el.id,
              className: el.className,
              rect: { left: rect.left, right: rect.right, width: rect.width }
            });
          }
        });
        return issues;
      });
    }

    // 4. Image Audit
    if (runAll || options.imagesOnly) {
      images = await page.evaluate(() => {
        return Array.from(document.querySelectorAll('img')).map(img => {
          const rect = img.getBoundingClientRect();
          return {
            src: img.src,
            alt: img.alt,
            naturalWidth: img.naturalWidth,
            naturalHeight: img.naturalHeight,
            renderedWidth: rect.width,
            renderedHeight: rect.height,
            loading: img.loading,
            isVisible: rect.top < window.innerHeight
          };
        });
      });
    }

    // 5. Tap Targets (Mobile only)
    if (runAll && viewport.width < 600) {
      tapTargets = await page.evaluate(() => {
        const interactive = document.querySelectorAll('button, a, input, select, textarea');
        return Array.from(interactive).map(el => {
          const rect = el.getBoundingClientRect();
          return {
            tagName: el.tagName,
            text: (el as HTMLElement).innerText?.substring(0, 20),
            width: rect.width,
            height: rect.height,
            isSmall: rect.width < 44 || rect.height < 44
          };
        }).filter(t => t.isSmall);
      });
    }

    // 6. Above the Fold
    if (runAll) {
      aboveTheFold = await page.evaluate(() => {
        const vh = window.innerHeight;
        const hero = document.querySelector('[data-section="hero"]') || document.querySelector('header');
        const heroRect = hero?.getBoundingClientRect();
        const ctas = Array.from(document.querySelectorAll('button, a.btn, .action-button')).filter(el => {
          const rect = el.getBoundingClientRect();
          return rect.top > 0 && rect.top < vh;
        });

        return {
          heroHeight: heroRect?.height,
          heroViewportPercentage: heroRect ? (heroRect.height / vh) * 100 : 0,
          visibleCTACount: ctas.length,
          hasPrimaryCTA: ctas.length > 0
        };
      });
    }

    const result: AuditResult = {
      route,
      viewport: viewport.name,
      timestamp: new Date().toISOString(),
      accessibility,
      overflow,
      images,
      tapTargets,
      aboveTheFold,
      screenshot: screenshotPath
    };

    const resultPath = path.join(resultsDir, `${slug}-${viewport.name}.json`);
    fs.writeFileSync(resultPath, JSON.stringify(result, null, 2));

  } catch (error) {
    console.error(`Failed to audit ${route}:`, error);
  } finally {
    await browser.close();
  }
}

// fallow-ignore-next-line complexity
async function main() {
  const args = process.argv.slice(2);
  const targetRoute = args[0] || '/';
  const targetViewportName = args.find(a => a.includes('desktop') || a.includes('mobile'));

  const imagesOnly = args.includes('--images-only');
  const overflowOnly = args.includes('--overflow-only');
  const contrastOnly = args.includes('--contrast-only');

  const { baseUrl, viewports } = loadUxConfig();
  const allViewports = [...viewports.desktop, ...viewports.mobile];

  const vps = targetViewportName
    ? allViewports.filter(v => v.name === targetViewportName)
    : allViewports;

  if (vps.length === 0 && targetViewportName) {
    console.error(`No viewports found matching ${targetViewportName}`);
    process.exit(1);
  }

  const activeVps = vps.length > 0 ? vps : allViewports;

  for (const vp of activeVps) {
    await runAudit(`${baseUrl}${targetRoute}`, targetRoute, vp, { imagesOnly, overflowOnly, contrastOnly });
  }
}

main().catch(console.error);
