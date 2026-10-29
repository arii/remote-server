#!/usr/bin/env bash
# Install the tracked git hooks into the S5 checkouts.
# Git never tracks .git/hooks, so hooks live in hooks/ (revision
# controlled) and this script copies them into place. Re-run after
# any fresh clone, and whenever hooks/ changes.
#
# Targets:
#   remote-server  -> <this repo>/.git/hooks/post-merge
#   tech-dancer    -> ~/repos/tech-dancer/.git/hooks/post-merge
#                    (override with TECH_DANCER_DIR)
# Usage:
#   scripts/setup-hooks.sh          install (copies + chmod +x)
#   scripts/setup-hooks.sh --check  report installed/missing/outdated
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TD_DIR="${TECH_DANCER_DIR:-$HOME/repos/tech-dancer}"
BT_DIR="${BOOMTICK_DIR:-$HOME/repos/boomtick}"
CHECK=0
[ "${1:-}" = "--check" ] && CHECK=1

install_one() { # source, dest-dir
  local src="$1" destdir="$2" dest="$2/post-merge"
  if [ ! -d "$destdir" ]; then
    echo "SKIP: $destdir is not a git checkout (.git missing)"
    return 0
  fi
  if [ "$CHECK" = 1 ]; then
    if [ ! -f "$dest" ]; then
      echo "MISSING: $dest"
    elif [ ! -x "$dest" ]; then
      echo "NOT-EXECUTABLE: $dest"
    elif ! cmp -s "$src" "$dest"; then
      echo "OUTDATED: $dest (re-run without --check)"
    else
      echo "OK: $dest"
    fi
    return 0
  fi
  cp "$src" "$dest"
  chmod +x "$dest"
  echo "installed: $dest"
}

install_one "$ROOT/hooks/post-merge" "$ROOT/.git/hooks"
install_one "$ROOT/hooks/post-merge-tech-dancer" "$TD_DIR/.git/hooks"
install_one "$ROOT/hooks/post-merge-tech-dancer" "$BT_DIR/.git/hooks"

if [ "$CHECK" = 0 ]; then
  echo "Done. Notes:"
  echo "- Automated pulls (webhook, Deploy buttons) skip hooks by design (-c core.hooksPath=/dev/null); hooks run only for interactive host pulls."
  echo "- The buttons container never rebuilds itself from a hook (see hooks/post-merge)."
fi
