// Console route discovery for `td ux` (#880).
//
// The console is a single-page app (state-based tabs, no URL router),
// so the route list is static. Writes artifacts/ux-audit/routes.json,
// which `td ux audit --all-routes` and the lighthouse runner read.
// Invoked by td with cwd=checkout:
//   pnpm exec tsx scripts/ux-discover-routes.ts
import fs from 'fs';
import path from 'path';

// Single-page console: one auditable route. If URL routing ever
// lands, extend this list (or scan the router) — keep "/" first.
const STATIC_ROUTES = ['/'];

async function discoverRoutes() {
  const outputDir = path.join(process.cwd(), 'artifacts', 'ux-audit');
  if (!fs.existsSync(outputDir)) {
    fs.mkdirSync(outputDir, { recursive: true });
  }

  const outputPath = path.join(outputDir, 'routes.json');
  fs.writeFileSync(
    outputPath,
    JSON.stringify({ routes: STATIC_ROUTES }, null, 2),
  );
  console.log(
    `Discovered ${STATIC_ROUTES.length} routes. Saved to ${outputPath}`,
  );
}

discoverRoutes().catch(console.error);
