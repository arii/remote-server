// fallow-ignore-file unused-file
// Playwright loads this by glob/config, not by import — unreachable in fallow's graph by design.
import { existsSync, mkdirSync, copyFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
export const e2eRoot = here;
export const repoRoot = join(here, '..', '..');
export const stateDir = join(here, '.state');
export const backendPort = 8931;
export const backendUrl = `http://127.0.0.1:${backendPort}`;

/** Seed the stub state + harness env. Runs once per `playwright test`. */
async function globalSetup(): Promise<void> {
  const dist = join(repoRoot, 'frontend', 'dist', 'index.html');
  if (!existsSync(dist)) {
    throw new Error(
      'frontend/dist is missing — build the SPA first so the backend serves it like prod:\n' +
        '  pnpm --filter frontend build   (from the repo root, or just run tests/e2e/run.sh)',
    );
  }
  mkdirSync(join(stateDir, 'repos'), { recursive: true });
  copyFileSync(join(here, 'fixtures', 'state', 'seed.json'), join(stateDir, 'gh-state.json'));
  writeFileSync(join(stateDir, 'e2e.env'), '# E2E harness compose env (stub docker ignores it).\n');
}

export default globalSetup;
