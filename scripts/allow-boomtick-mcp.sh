#!/usr/bin/env bash
set -euo pipefail

# Deprecation wrapper: allow-boomtick-mcp.sh has been renamed to update-permissions.sh.
echo "==> scripts/allow-boomtick-mcp.sh is deprecated, calling scripts/update-permissions.sh instead..." >&2
exec "$(dirname "$0")/update-permissions.sh" "$@"
