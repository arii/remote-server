#!/usr/bin/env bash
# One-command local run for the console E2E smoke suite (issue #854):
#   tests/e2e/run.sh
#
# Builds the frontend (the backend serves dist/ like prod), installs
# the suite's own deps + chromium on first run, seeds the stub
# state, and runs Playwright against a live backend on 127.0.0.1:8931.
# Set E2E_REBUILD=1 to force a fresh frontend build.
set -euo pipefail

E2E_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$E2E_DIR/../.." && pwd)"

if [ ! -f "$REPO_ROOT/frontend/dist/index.html" ] || [ "${E2E_REBUILD:-}" = "1" ]; then
  echo "==> building frontend SPA" >&2
  (cd "$REPO_ROOT" && pnpm install --frozen-lockfile && pnpm --filter frontend build)
fi

if [ ! -d "$E2E_DIR/node_modules" ]; then
  echo "==> installing e2e deps" >&2
  (cd "$E2E_DIR" && npm ci)
fi

if ! python3 -c "import fastapi, uvicorn" 2>/dev/null; then
  echo "==> installing backend deps" >&2
  pip install -r "$REPO_ROOT/backend/requirements.txt"
fi

echo "==> ensuring chromium" >&2
(cd "$E2E_DIR" && npx playwright install --with-deps chromium)

echo "==> running suite" >&2
(cd "$E2E_DIR" && npx playwright test "$@")
