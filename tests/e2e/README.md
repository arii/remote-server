# Console E2E smoke suite

Playwright specs proving the console-critical flows against a live
buttons stack: agent launch→turn→abort, the approve→merge gate
matrix (including the #848 self-approval lock), review-pane
merge-block context, and job trigger→green run.

## Run it (one command)

```sh
tests/e2e/run.sh
```

That builds the frontend SPA, installs the suite's own deps plus
chromium on first run, seeds the stub state, and runs Playwright
against a real backend (`uvicorn backend.server:app`) on
`http://127.0.0.1:8931` — the backend serves `frontend/dist`
itself, exactly like the production image. Set `E2E_REBUILD=1`
to force a fresh SPA build.

## How it stays hermetic (no product-code changes)

The suite touches **no product code**. Hermeticity comes from two
`PATH` stubs in `fixtures/bin/` plus the backend's own env seams:

- `gh` — canned PR/issue answers from `.state/gh-state.json`
  (re-seeded every run from `fixtures/state/seed.json`).
  Approve/merge mutate the rows, so list refreshes observe the
  change. Self-approval fails with GitHub's own GraphQL error,
  which is what the #848 lock asserts the backend translates to
  a readable 400 instead of a 500.
- `docker` — provider probes report present; `muse exec` (the
  mock provider seam) prints a canned answer, or a slow drip
  when the prompt carries `SLOWTURN` so the abort spec has a
  live turn to kill; `td doctor` prints a canned green report.
- Env: `BUTTONS_STACK`/`BACKEND_STACK_DIR` point at the
  checkout, every store path (`*_PATH`) points at `.state/`.

Unknown stub argv fails LOUD (exit 1): a backend call the
harness does not model reddens the suite instead of sliding
through as a silent pass.

## Layout

- `tests/` — one spec file per flow; serial, one worker (the
  stub gh state is shared, so parallelism would race rows).
  New spec/global files must start with the bare
  `// fallow-ignore-file unused-file` directive (Playwright
  loads them by glob, invisible to the dead-code audit).
- `fixtures/bin/` — the `gh` + `docker` stubs.
- `fixtures/state/seed.json` — PRs 101–103, one issue, viewer.
- `playwright.config.ts` / `global-setup.ts` — backend
  lifecycle + seeding. No retries anywhere: a flake must
  redden the run, not hide behind a second attempt.

## Debugging

- `.state/gh.log` + `.state/docker.log` record every stub call.
- `npx playwright test --headed` / `--debug` from `tests/e2e`
  (with the backend already up, the config reuses it locally).
- `npx playwright show-report` after a failure.

## CI

`.github/workflows/e2e.yml` runs the suite on PRs touching
`frontend/`, `backend/`, or the suite itself. Deliberately
**non-required** until three consecutive green runs prove it
stable — only then should it become a required context.
