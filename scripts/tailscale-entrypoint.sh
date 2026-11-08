#!/bin/sh
# Entrypoint for the tailscale container: start tailscaled and join the tailnet
# as a VPN for admin access (SSH, future services). Outbound only.
#
# NOTE: the web terminal does NOT go through Tailscale — it is served publicly
# via Ari's DDNS + certbot on ${TERMINAL_PORT} (see docker-compose.yml).
# If the public port is ever blocked (restrictive hotel/cafe network),
# set TS_SERVE=1 to expose the edge over the tailnet instead:
# https://<machine>.<tailnet>.ts.net in the phone browser (Tailscale app
# on). Default off: VPN-only. No funnel — the tailnet is the boundary.
#
# Requires a REUSABLE TS_AUTHKEY (entrypoint re-runs `tailscale up` on restart).
set -eu

: "${TS_AUTHKEY:?TS_AUTHKEY is required (reusable auth key)}"
TS_HOSTNAME="${TS_HOSTNAME:-s5}"

/usr/local/bin/tailscaled \
  --state=/var/lib/tailscale/tailscaled.state \
  --socket=/var/run/tailscale/tailscaled.sock &
TAILSCALED_PID=$!

# wait for the socket
for _ in $(seq 1 30); do
  [ -S /var/run/tailscale/tailscaled.sock ] && break
  sleep 1
done

tailscale up \
  --authkey="${TS_AUTHKEY}" \
  --hostname="${TS_HOSTNAME}" \
  --accept-dns=false

echo "tailscale up as: $(tailscale ip -4)"
tailscale status || true

# Opt-in tailnet serving (default off). TS_SERVE=1 exposes the edge over
# https://<machine>.<tailnet>.ts.net/ for networks where the public DDNS
# port is blocked. Target defaults to the oauth2-proxy public listener;
# override with TS_SERVE_TARGET (any http:// URL reachable from this
# host-network container). Idempotent: re-running serve re-applies it.
if [ -n "${TS_SERVE:-}" ]; then
  : "${TERMINAL_PORT:?TS_SERVE=1 needs TERMINAL_PORT}"
  SERVE_TARGET="${TS_SERVE_TARGET:-http://127.0.0.1:${TERMINAL_PORT}}"
  tailscale serve --https=443 --set-path / "$SERVE_TARGET"
  echo "tailscale serving $SERVE_TARGET"
  tailscale serve status || true
fi

wait "$TAILSCALED_PID"
