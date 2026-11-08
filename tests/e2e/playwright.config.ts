import { defineConfig } from '@playwright/test';
import { join } from 'node:path';
import { backendUrl, backendPort, e2eRoot, repoRoot, stateDir } from './global-setup.js';

// Console E2E smoke suite (issue #854): one worker, serial specs.
// The stub gh backend is stateful (approve/merge mutate it), so
// parallel workers would race each other's PR rows. Four fast
// specs in one worker stay quicker than a parallel suite with
// per-worker backends — and match the smoke-suite budget.
const webEnv: Record<string, string> = {
  ...process.env as Record<string, string>,
  PATH: `${join(e2eRoot, 'fixtures', 'bin')}:${process.env.PATH ?? ''}`,
  // Stub + harness state.
  E2E_STATE_FILE: join(stateDir, 'gh-state.json'),
  E2E_GH_LOG: join(stateDir, 'gh.log'),
  E2E_DOCKER_LOG: join(stateDir, 'docker.log'),
  // Entry-point path for the backend child (no in-code sys.path hacks):
  // repo root for backend.*, deep-agent/ for s5tools/runs/impact.
  PYTHONPATH: `${repoRoot}:${join(repoRoot, 'deep-agent')}${process.env.PYTHONPATH ? `:${process.env.PYTHONPATH}` : ''}`,
  // Point every backend store/path seam at the checkout + tmp state.
  BUTTONS_STACK: repoRoot,
  BACKEND_STACK_DIR: repoRoot,
  BUTTONS_REPOS_DIR: join(stateDir, 'repos'),
  BUTTONS_COMPOSE_FILE: join(repoRoot, 'docker-compose.yml'),
  BUTTONS_ENV_FILE: join(stateDir, 'e2e.env'),
  BUTTONS_LEDGER_PATH: join(stateDir, 'ledger.jsonl'),
  BUTTONS_SNOOZE_PATH: join(stateDir, 'attention_snooze.json'),
  BUTTONS_DEFERRED_PATH: join(stateDir, 'deferred.json'),
  BUTTONS_JOBS_RUNS_PATH: join(stateDir, 'jobs_runs.json'),
};

export default defineConfig({
  testDir: './tests',
  fullyParallel: false,
  workers: 1,
  // No retries, anywhere: a flake must redden the run, not hide
  // behind a second attempt (DoD: 3 consecutive green runs).
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI ? [['github'], ['html', { open: 'never' }]] : 'list',
  globalSetup: './global-setup.ts',
  use: { baseURL: backendUrl, trace: 'retain-on-failure' },
  webServer: {
    command: `python3 -m uvicorn backend.server:app --host 127.0.0.1 --port ${backendPort}`,
    cwd: repoRoot,
    env: webEnv,
    url: `${backendUrl}/health`,
    timeout: 120_000,
    reuseExistingServer: !process.env.CI,
  },
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
});
