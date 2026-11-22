#!/bin/sh
# buttons-volume-check.sh - read-only check of the Buttons state volume (#1517).
#
# /var/lib/buttons is a CONTAINER path backed by the named volume buttons-data
# (remote_buttons-data on S5). Run this from /srv/remote-server inside the
# terminal container (the buttons image does not ship scripts/), never on the
# host: the host has no such
# directory by design. See docs/buttons.md "State volume".
#
# Verifies the directory is readable and each required state file exists and
# is readable. Writes nothing. Exit 0 only when every check passes.
#
# Usage: sh scripts/buttons-volume-check.sh
#   BUTTONS_DATA_DIR        directory to check (default /var/lib/buttons)
#   BUTTONS_REQUIRED_STATE  space-separated file names (default jobs_schedules.json)
set -u
dir=${BUTTONS_DATA_DIR:-/var/lib/buttons}
required=${BUTTONS_REQUIRED_STATE:-jobs_schedules.json}
fail=0

if [ -d "$dir" ] && [ -r "$dir" ]; then
  echo "PASS: $dir is a readable directory (owner uid:gid $(stat -c '%u:%g' "$dir"))"
else
  echo "FAIL: $dir is missing or unreadable (run inside the terminal container, not on the host)"
  exit 1
fi

for name in $required; do
  if [ -f "$dir/$name" ] && [ -r "$dir/$name" ]; then
    echo "PASS: $dir/$name is readable ($(stat -c '%s bytes, owner %u:%g' "$dir/$name"))"
  else
    echo "FAIL: $dir/$name is missing or unreadable"
    fail=1
  fi
done

[ "$fail" -eq 0 ]
