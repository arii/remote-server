#!/usr/bin/env bash
# certbot --deploy-hook: restart oauth2-proxy so it picks up renewed certs,
# then verify it actually came back healthy (#527).
#
# oauth2-proxy reads its TLS cert/key once at startup, and the cert files are
# mounted read-only from the host certbot dir — so after certbot renews,
# the proxy must restart to serve the new certificate.
#
# The pre-restart checks only prove docker ACCEPTED the restart; a proxy that
# crash-loops afterwards (bad renewed cert, config error) used to report
# success while the public TLS edge served nothing. So after `docker
# restart` we poll (bounded, ~60s) until every restarted container reports
# running AND the edge answers a TLS probe — and exit nonzero with a
# diagnostic when it never does, so certbot reports the hook failure.
#
# Installed by scripts/install-certbot-deploy-hook.sh into
# /etc/letsencrypt/renewal-hooks/deploy/. certbot runs deploy-hooks only when
# a certificate was actually renewed (not on every renew attempt).
#
# All knobs are env-overridable:
#   CERTBOT_HOOK_URL       full probe URL (default: built from .env)
#   REMOTE_SERVER_DIR      host checkout holding .env (/home/ari/remote-server)
#   CERTBOT_HOOK_TIMEOUT   total seconds to wait for health after restart (60)
#   CERTBOT_HOOK_INTERVAL  seconds between health polls (3)
set -euo pipefail

if ! command -v docker >/dev/null 2>&1; then
  echo "docker not found; skipping oauth2-proxy restart" >&2
  exit 0
fi

ids=$(docker ps -q --filter "name=oauth2-proxy" || true)
if [[ -z "$ids" ]]; then
  echo "no oauth2-proxy container running; nothing to restart"
  exit 0
fi

# shellcheck disable=SC2086  # intentional word splitting: one arg per container id
docker restart $ids
echo "restarted oauth2-proxy for renewed certificates"

# --- post-restart verification (#527) ---
timeout_s="${CERTBOT_HOOK_TIMEOUT:-60}"
interval_s="${CERTBOT_HOOK_INTERVAL:-3}"

probe_url="${CERTBOT_HOOK_URL:-}"
if [[ -z "$probe_url" ]]; then
  checkout="${REMOTE_SERVER_DIR:-/home/ari/remote-server}"
  domain=""
  port=""
  if [[ -f "$checkout/.env" ]]; then
    domain=$(grep -E '^TERMINAL_DOMAIN=' "$checkout/.env" | cut -d= -f2 | tr -d '"' || true)
    port=$(grep -E '^TERMINAL_PORT=' "$checkout/.env" | cut -d= -f2 | tr -d '"' || true)
  fi
  domain="${domain:-${TERMINAL_DOMAIN:-}}"
  port="${port:-${TERMINAL_PORT:-}}"
  if [[ -n "$domain" && -n "$port" ]]; then
    probe_url="https://$domain:$port/ping"
  fi
fi

if [[ -z "$probe_url" ]]; then
  echo "warning: no probe target (set CERTBOT_HOOK_URL or TERMINAL_DOMAIN/TERMINAL_PORT); verifying container state only" >&2
fi

containers_running() {
  local id state
  # shellcheck disable=SC2086  # intentional word splitting: one id per line of $ids
  for id in $ids; do
    state=$(docker inspect -f '{{.State.Running}}' "$id" 2>/dev/null || echo "?")
    [[ "$state" == "true" ]] || return 1
  done
  return 0
}

edge_serving() {
  [[ -n "$probe_url" ]] || return 0
  curl -fsS -o /dev/null --max-time "$interval_s" "$probe_url" 2>/dev/null
}

served_expiry() {
  case "$probe_url" in
    https://*) ;;
    *) return 0 ;;
  esac
  if ! command -v openssl >/dev/null 2>&1; then
    echo "warning: openssl not found; skipping served-cert expiry check" >&2
    return 0
  fi
  local hostport host port expiry
  hostport="${probe_url#https://}"
  hostport="${hostport%%/*}"
  host="${hostport%%:*}"
  port=443
  [[ "$hostport" == *:* ]] && port="${hostport##*:}"
  expiry=$(openssl s_client -connect "$host:$port" -servername "$host" </dev/null 2>/dev/null \
    | openssl x509 -noout -enddate 2>/dev/null || true)
  if [[ -n "$expiry" ]]; then
    echo "oauth2-proxy serving renewed certificate: $expiry"
  else
    echo "warning: could not read served certificate expiry from $host:$port" >&2
  fi
}

deadline=$((SECONDS + timeout_s))
last_state="health check never ran"
while (( SECONDS < deadline )); do
  if ! containers_running; then
    last_state="container not running after restart"
  elif ! edge_serving; then
    last_state="container running but edge not serving $probe_url"
  else
    served_expiry
    exit 0
  fi
  sleep "$interval_s"
done

echo "ERROR: oauth2-proxy unhealthy ${timeout_s}s after restart ($last_state); renewed cert may not be live" >&2
exit 1
