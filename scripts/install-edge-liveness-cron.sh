#!/usr/bin/env bash
# Install the edge liveness watcher (#537) as a host cron job.
# Idempotent — safe to re-run. Run once as root on the S5 host.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "run as root (sudo)" >&2; exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
install -m 0755 "$script_dir/edge-liveness-watch.sh" /usr/local/bin/edge-liveness-watch

# Every minute; restart fires after 3 consecutive failures, so a wedged
# edge is detected and self-healed within ~3 minutes, day or night.
cat > /etc/cron.d/edge-liveness-watch <<'EOF'
# TLS-edge liveness probe (#537). Managed by scripts/install-edge-liveness-cron.sh.
* * * * * root /usr/local/bin/edge-liveness-watch
EOF
chmod 0644 /etc/cron.d/edge-liveness-watch

echo "installed: /usr/local/bin/edge-liveness-watch + /etc/cron.d/edge-liveness-watch"
echo "smoke-test with: EDGE_LIVENESS_URL=https://\$TERMINAL_DOMAIN:\$TERMINAL_PORT/ping /usr/local/bin/edge-liveness-watch"
echo "restarts are logged to cron output and syslog (journalctl -t edge-liveness)"
