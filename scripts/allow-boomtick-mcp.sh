#!/usr/bin/env bash
# Deprecation wrapper: permissions are canonically managed by scripts/update-permissions.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "Notice: allow-boomtick-mcp.sh is deprecated. Invoking scripts/update-permissions.sh..." >&2
exec "${SCRIPT_DIR}/update-permissions.sh" "$@"
