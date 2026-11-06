#!/usr/bin/env bash
# Canonical buttons-only bring-up. Never run a bare full-stack
# `docker compose up -d` from inside the terminal container: recreating
# the terminal service kills the shell you are running in (and every
# session inside it). This wrapper only ever (re)builds and restarts
# the buttons service; anything else is refused.
set -euo pipefail

for arg in "$@"; do
  if [[ "$arg" == "terminal" ]]; then
    echo "refused: never restart/recreate the terminal service (use buttons only)" >&2
    exit 1
  fi
done

# Default to the buttons service when no service is named.
svc="buttons"
for arg in "$@"; do
  case "$arg" in
    -*) continue ;;
    *) svc="$arg"; break ;;
  esac
done
if [[ "$svc" != "buttons" ]]; then
  echo "refused: this wrapper only brings up the buttons service (got: $svc)" >&2
  exit 1
fi

exec docker compose up -d --build buttons
