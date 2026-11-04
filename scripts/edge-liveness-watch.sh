#!/usr/bin/env bash
# Edge liveness watcher (#537): probe the public TLS edge and self-heal a
# wedged-but-alive oauth2-proxy (accepting TLS, failing all proxied
# requests) by restarting it after N consecutive probe failures.
#
# Why host-side, not a compose healthcheck: the oauth2-proxy image ships no
# shell/curl, so an in-container CMD-SHELL probe is genuinely awkward — but
# GET /ping answers 200 OK unauthenticated, so a host curl probe is trivial.
# Docker `restart:` only fires on process exit; this covers the live-but-dark
# wedge (bad renewal, upstream hang) that never exits on its own.
#
# Installed by scripts/install-edge-liveness-cron.sh (runs every minute from
# /etc/cron.d). Safe to run by hand. All knobs are env-overridable:
#   EDGE_LIVENESS_URL          full probe URL (default: built from .env)
#   REMOTE_SERVER_DIR          host checkout holding .env (/home/ari/remote-server)
#   EDGE_LIVENESS_STATE        consecutive-failure counter file
#   EDGE_LIVENESS_MAX_FAILURES restart after this many consecutive failures (3)
#   EDGE_LIVENESS_TIMEOUT      per-probe curl timeout in seconds (10)
set -euo pipefail

checkout="${REMOTE_SERVER_DIR:-/home/ari/remote-server}"
state_file="${EDGE_LIVENESS_STATE:-/var/tmp/edge-liveness.failures}"
max_failures="${EDGE_LIVENESS_MAX_FAILURES:-3}"
timeout_s="${EDGE_LIVENESS_TIMEOUT:-10}"

log() { echo "edge-liveness: $*"; }
journal() { command -v logger >/dev/null 2>&1 && logger -t edge-liveness "$*" || true; }

probe_url="${EDGE_LIVENESS_URL:-}"
if [[ -z "$probe_url" ]]; then
  domain=""
  port=""
  if [[ -f "$checkout/.env" ]]; then
    domain=$(grep -E '^TERMINAL_DOMAIN=' "$checkout/.env" | cut -d= -f2 | tr -d '"' || true)
    port=$(grep -E '^TERMINAL_PORT=' "$checkout/.env" | cut -d= -f2 | tr -d '"' || true)
  fi
  domain="${domain:-${TERMINAL_DOMAIN:-}}"
  port="${port:-${TERMINAL_PORT:-}}"
  if [[ -z "$domain" || -z "$port" ]]; then
    log "no probe target: set EDGE_LIVENESS_URL or TERMINAL_DOMAIN/TERMINAL_PORT" >&2
    exit 1
  fi
  probe_url="https://$domain:$port/ping"
fi

if curl -fsS -o /dev/null --max-time "$timeout_s" "$probe_url" 2>/dev/null; then
  rm -f "$state_file"
  exit 0
fi

prev=0
if [[ -f "$state_file" ]]; then
  prev=$(cat "$state_file" 2>/dev/null || echo 0)
  [[ "$prev" =~ ^[0-9]+$ ]] || prev=0
fi
failures=$((prev + 1))
echo "$failures" > "$state_file"

if (( failures < max_failures )); then
  log "probe $probe_url failed ($failures/$max_failures); waiting for confirmation"
  exit 0
fi

# Threshold reached: restart the edge and ledger the event.
reason="edge liveness probe $probe_url failed $failures consecutive times"
if ! command -v docker >/dev/null 2>&1; then
  log "$reason; docker not found, cannot restart" >&2
  journal "$reason; docker not found, cannot restart"
  exit 1
fi
ids=$(docker ps -q --filter "name=oauth2-proxy" || true)
if [[ -z "$ids" ]]; then
  log "$reason; no oauth2-proxy container running, nothing to restart"
  journal "$reason; no oauth2-proxy container running, nothing to restart"
  rm -f "$state_file"
  exit 0
fi
# shellcheck disable=SC2086  # intentional word splitting: one arg per container id
docker restart $ids >/dev/null
log "$reason; restarted oauth2-proxy ($ids)"
journal "$reason; restarted oauth2-proxy ($ids)"
rm -f "$state_file"
