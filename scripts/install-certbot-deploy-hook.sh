#!/usr/bin/env bash
# Install the certbot deploy-hook that restarts oauth2-proxy when certs renew.
# Idempotent — safe to re-run. Run once as root AFTER certbot is working
# (Ari's certbot is pre-existing; this only registers the hook).
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "run as root (sudo)" >&2; exit 1
fi

hook_dir=/etc/letsencrypt/renewal-hooks/deploy
if [[ ! -d "$hook_dir" ]]; then
  echo "no $hook_dir — is certbot installed and has it issued a cert?" >&2
  exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
install -m 0755 "$script_dir/certbot-deploy-hook.sh" "$hook_dir/remote-server-oauth2-proxy"

echo "installed deploy-hook: $hook_dir/remote-server-oauth2-proxy"
echo "verify with: certbot renew --dry-run   (hook runs only on real renewal)"
echo "or run the hook directly: $hook_dir/remote-server-oauth2-proxy"
