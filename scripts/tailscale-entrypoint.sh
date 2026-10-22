#!/usr/bin/env bash
# Entrypoint for the tailscale container: start tailscaled and join the tailnet
# as a VPN for admin access (SSH, future services). Outbound only.
#
# NOTE: the web terminal does NOT go through Tailscale — it is served publicly
# via Ari's DDNS + certbot on ${TERMINAL_PORT} (see docker-compose.yml).
# If the public port is ever blocked (restrictive hotel/cafe network), the
# manual fallback is: docker exec <tailscale-container> tailscale serve \
#   --https=443 --set-path / http://127.0.0.1:${TERMINAL_PORT}
# then open https://<machine>.<tailnet>.ts.net in the phone browser
# (Tailscale app on).
#
# Requires a REUSABLE TS_AUTHKEY (entrypoint re-runs `tailscale up` on restart).
set -euo pipefail

: "${TS_AUTHKEY:?TS_AUTHKEY is required (reusable auth key)}"
TS_HOSTNAME="${TS_HOSTNAME:-s5}"

/usr/local/bin/tailscaled \
  --state=/var/lib/tailscale/tailscaled.state \
  --socket=/var/run/tailscale/tailscaled.sock &
TAILSCALED_PID=$!

# wait for the socket
for _ in $(seq 1 30); do
  [[ -S /var/run/tailscale/tailscaled.sock ]] && break
  sleep 1
done

tailscale up \
  --authkey="${TS_AUTHKEY}" \
  --hostname="${TS_HOSTNAME}" \
  --accept-dns=false

echo "tailscale up as: $(tailscale ip -4)"
tailscale status || true

wait "$TAILSCALED_PID"
