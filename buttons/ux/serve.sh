#!/usr/bin/env bash
# Local UX preview for the buttons page. Starts buttons/server.py
# on 127.0.0.1:18099 with a gitignored scratch ledger and seed repos,
# so the page can be previewed or screenshotted without touching real data.
#
# Usage: ./buttons/ux/serve.sh [port]   (default 18099)
# Then open http://127.0.0.1:<port>/buttons
#
# PKILL TRAP WARNING:
# NEVER run `pkill -f python` or `pkill -f server` to stop this preview!
# In many agent and remote environments, doing so matches and terminates your
# own shell session, runner, or subagent parent processes.
# To stop this server:
#   - Press Ctrl+C if running in foreground
#   - Or kill by port / PID:
#       kill $(lsof -t -i :<port>) 2>/dev/null || true
#       kill $(cat buttons/ux/.scratch-server.pid 2>/dev/null) 2>/dev/null || true
set -euo pipefail

PORT="${1:-18099}"
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
UX_DIR="$HERE/buttons/ux"
SEED_DIR="${UX_SEED_DIR:-$UX_DIR/.scratch-seed}"
LEDGER="${UX_LEDGER:-$UX_DIR/scratch-ledger.json}"
FIXTURE="${UX_FIXTURE_BRIEFING:-$UX_DIR/.scratch-briefing.json}"
PIDFILE="$UX_DIR/.scratch-server.pid"

mkdir -p "$SEED_DIR"

# Terminate any existing preview process on this specific port safely.
if command -v lsof >/dev/null 2>&1; then
  EXISTING_PID="$(lsof -t -i ":$PORT" 2>/dev/null || true)"
  if [ -n "$EXISTING_PID" ]; then
    echo "Stopping existing process on port $PORT (PID $EXISTING_PID)..."
    kill "$EXISTING_PID" 2>/dev/null || true
    sleep 1
  fi
elif [ -f "$PIDFILE" ]; then
  OLD_PID="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "Stopping previous server PID $OLD_PID..."
    kill "$OLD_PID" 2>/dev/null || true
    sleep 1
  fi
fi

# Populate realistic preview seed data and fixture briefing
python3 "$UX_DIR/seed.py" "$SEED_DIR" "$LEDGER" "$FIXTURE"

echo "Starting buttons preview server on http://127.0.0.1:$PORT/buttons"
echo "(Safe stop: kill \$(lsof -t -i :$PORT) or Ctrl+C. Do NOT run blind pkill python!)"

BUTTONS_PORT="$PORT" \
BUTTONS_HOST="127.0.0.1" \
BUTTONS_LEDGER_PATH="$LEDGER" \
BUTTONS_REPOS_DIR="$SEED_DIR" \
BUTTONS_FIXTURE_BRIEFING="$FIXTURE" \
  exec python3 "$HERE/buttons/server.py"
