#!/usr/bin/env bash
# Local UX preview for the buttons page. Starts buttons/server.py
# with a fake repos dir and a throwaway ledger, so the page can
# be screenshotted without touching the real stack.
#
# Usage: ./serve.sh [port]   (default 18099)
# Then open http://localhost:<port>/buttons
set -euo pipefail
PORT="${1:-18099}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SEED_DIR="${UX_SEED_DIR:-/tmp/buttons-ux-seed}"
LEDGER="${UX_LEDGER:-/tmp/buttons-ux-ledger.json}"

mkdir -p "$SEED_DIR"
python3 "$HERE/buttons/ux/seed.py" "$SEED_DIR" "$LEDGER"

BUTTONS_PORT="$PORT" \
BUTTONS_LEDGER_PATH="$LEDGER" \
BUTTONS_REPOS_DIR="$SEED_DIR" \
  exec python3 "$HERE/buttons/server.py"
