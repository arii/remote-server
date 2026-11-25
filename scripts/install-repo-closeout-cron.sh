#!/usr/bin/env bash
# Run on the actual managed host, not the code-server container.
# No services are restarted. Existing schedules are left untouched.
set -euo pipefail
[[ $EUID == 0 ]] || { echo 'Run as root on the managed host'; exit 1; }
command -v crontab >/dev/null || { echo 'No cron installation found; nothing installed'; exit 1; }
command -v flock >/dev/null || { echo 'flock required; nothing installed'; exit 1; }
command -v python3 >/dev/null || { echo 'Python 3 required; nothing installed'; exit 1; }
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="${CLOSEOUT_USER:?Set CLOSEOUT_USER to the account that owns these repos}"
id "$USER_NAME" >/dev/null
[[ "$ROOT" =~ ^/[a-zA-Z0-9_./-]+$ ]] || { echo 'Unsupported checkout path'; exit 1; }
# Explicit repository list, no discovery or traversal of unrelated checkouts.
for repo in "$ROOT" /srv/repos/boomtick /srv/repos/sf-promises /srv/repos/posture-guard; do
  [[ -d "$repo/.git" || -f "$repo/.git" ]] || { echo "Missing checkout: $repo"; exit 1; }
  [[ "$(stat -c %U "$repo")" == "$USER_NAME" ]] || { echo "Ownership mismatch: $repo"; exit 1; }
done
# Refuse to replace an existing schedule from another installation.
DEST=/etc/cron.d/repo-closeout
[[ ! -e "$DEST" ]] || { echo "$DEST already exists; inspect it before changing"; exit 1; }
PYTHON="$(command -v python3)"
cat > "$DEST" <<EOF
# Safe repo closeout. Every 30 minutes; no commits, resets or service restarts.
SHELL=/bin/bash
PATH=/usr/local/bin:/usr/bin:/bin
*/30 * * * * $USER_NAME /usr/bin/flock -n /tmp/repo-closeout-$USER_NAME.lock $PYTHON $ROOT/scripts/repo-closeout.py --apply $ROOT /srv/repos/boomtick /srv/repos/sf-promises /srv/repos/posture-guard >> /tmp/repo-closeout-$USER_NAME.log 2>&1
EOF
chmod 0644 "$DEST"
echo "Installed $DEST. Run closeout manually once, then verify a scheduled log entry."
