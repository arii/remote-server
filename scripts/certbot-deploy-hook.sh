#!/usr/bin/env bash
# certbot --deploy-hook: restart oauth2-proxy so it picks up renewed certs.
#
# oauth2-proxy reads its TLS cert/key once at startup, and the cert files are
# mounted read-only from the host certbot dir — so after certbot renews,
# the proxy must restart to serve the new certificate.
#
# Installed by scripts/install-certbot-deploy-hook.sh into
# /etc/letsencrypt/renewal-hooks/deploy/. certbot runs deploy-hooks only when
# a certificate was actually renewed (not on every renew attempt).
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
