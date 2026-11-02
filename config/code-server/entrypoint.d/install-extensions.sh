#!/bin/sh
# Boot hook for code-server, run from the image entrypoint via $ENTRYPOINTD
# (see /usr/bin/entrypoint.sh in codercom/code-server). Installs every
# extension listed in the sibling extensions.txt unless already installed,
# then returns so the entrypoint can start the server. MUST always exit 0:
# a nonzero exit would abort the image entrypoint (set -e) and kill the
# container, so per-extension failures are logged, never fatal.
set -eu

LIST="$(dirname "$0")/extensions.txt"
INSTALLED="$(code-server --list-extensions 2>/dev/null || true)"

grep -v -e '^#' -e '^[[:space:]]*$' "$LIST" | while read -r ext _; do
  [ -n "${ext:-}" ] || continue
  case "$INSTALLED" in
    *"$ext"*)
      echo "entrypoint.d: already installed: $ext"
      ;;
    *)
      echo "entrypoint.d: installing: $ext"
      code-server --install-extension "$ext" || echo "entrypoint.d: FAILED (continuing): $ext"
      ;;
  esac
done

exit 0
