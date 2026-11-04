// Console Lighthouse runner for `td ux lighthouse` (#880).
//
// Vendored from tech-dancer scripts/ux-lighthouse-runner.ts. Config
// loads through ./ux-config (UX_BASE_URL override included). Reads
// the discovered routes and audits the first static ones.
// Invoked by td with cwd=checkout:
//   pnpm exec tsx scripts/ux-lighthouse-runner.ts
// fallow-ignore-file unused-file
import { execFileSync } from 'child_process';
import fs from 'fs';
import path from 'path';
import { loadUxConfig } from './ux-config';

// Vendored runner shape: kept line-faithful to upstream so future
// reference syncs stay clean (see file header).
// fallow-ignore-next-line complexity
async function runLighthouse() {
  const routesPath = path.join(process.cwd(), 'artifacts', 'ux-audit', 'routes.json');
  if (!fs.existsSync(routesPath)) {
    console.error('routes.json not found. Run route discovery first.');
    process.exit(1);
  }

  const { routes } = JSON.parse(fs.readFileSync(routesPath, 'utf-8'));
  const { baseUrl } = loadUxConfig();

  const lighthouseDir = path.join(process.cwd(), 'artifacts', 'ux-audit', 'lighthouse');
  if (!fs.existsSync(lighthouseDir)) {
    fs.mkdirSync(lighthouseDir, { recursive: true });
  }

  // We'll limit to a subset of important routes if there are too many,
  // or just run for the ones requested. For now, let's just do a few key ones.
  const keyRoutes = routes.filter((r: string) => !r.includes(':') && routes.indexOf(r) < 10);

  for (const route of keyRoutes) {
    const url = `${baseUrl}${route}`;
    const slug = route.replace(/\//g, '_') || 'home';
    const reportPath = path.join(lighthouseDir, slug);

    console.log(`Running Lighthouse for ${url}...`);
    try {
      // Sanitize and validate inputs against CodeQL command/argument injection
      if (!/^https?:\/\/[a-zA-Z0-9][-a-zA-Z0-9@:%._+~#=]{1,256}(\.[a-zA-Z0-9()]{1,6})?\b([-a-zA-Z0-9()@:%_+.~#?&//=]*)$/.test(url)) {
        throw new Error(`Invalid URL format: ${url}`);
      }
      if (!/^[\w\-/:\\.]+$/.test(reportPath)) {
        throw new Error(`Invalid report path format: ${reportPath}`);
      }

      // Using lhci if available, otherwise fallback to lighthouse CLI if installed globally/locally
      // Here we assume lhci autorun might be overkill for a specific route runner,
      // but let's use the lighthouse CLI directly.
      execFileSync('npx', [
        'lighthouse',
        url,
        '--output=json',
        '--output=html',
        `--output-path=${reportPath}`,
        '--chrome-flags=--headless',
        '--only-categories=performance,accessibility,best-practices,seo'
      ], { stdio: 'inherit' });
    } catch (error) {
      console.error(`Lighthouse failed for ${route}:`, error);
    }
  }
}

runLighthouse().catch(console.error);
