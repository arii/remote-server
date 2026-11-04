#!/usr/bin/env bash
# buttons entrypoint: fresh RAG index at startup (#562).
#
# The image carries no chroma_db (dockerignored: a baked copy would be
# stale the moment docs drift). On container (re)create the index dir
# is missing, so this entrypoint builds it from the baked code with
# the agent venv python, then execs the CMD (uvicorn). A plain
# `restart` keeps the container filesystem, so the present-index fast
# path skips the rebuild (the index still matches the image's code).
#
# Never empty-silent: when the index is missing and cannot be built
# (Ollama down past the wait budget, or the build itself fails), the
# entrypoint exits 1 instead of serving empty retrieval; compose's
# restart policy retries the whole sequence.
#
# Env (all optional): APP_ROOT (/app), DEEP_AGENT_VENV (agent python),
# S5_CHROMA_DIR (index dir), OLLAMA_URL, OLLAMA_WAIT_SECS (5; sleep
# between Ollama readiness probes, 36 tries), SKIP_INDEX_BUILD=1
# (one-off shells via `docker compose run`).
set -euo pipefail

APP_ROOT="${APP_ROOT:-/app}"
VENV="${DEEP_AGENT_VENV:-/opt/venvs/td/bin/python}"
INDEX_DIR="${S5_CHROMA_DIR:-$APP_ROOT/deep-agent/chroma_db}"
OLLAMA="${OLLAMA_URL:-http://ollama:11434}"
WAIT_SECS="${OLLAMA_WAIT_SECS:-5}"
TRIES=36

if [[ "${SKIP_INDEX_BUILD:-}" == "1" ]]; then
  echo "buttons-entrypoint: SKIP_INDEX_BUILD=1, skipping index check"
  exec "$@"
fi

if [[ -s "$INDEX_DIR/chroma.sqlite3" ]]; then
  echo "buttons-entrypoint: index present at $INDEX_DIR, skipping rebuild"
  exec "$@"
fi

echo "buttons-entrypoint: building RAG index into $INDEX_DIR"
i=0
until curl -fsS --max-time 5 "$OLLAMA/api/tags" > /dev/null; do
  i=$((i + 1))
  if [[ "$i" -ge "$TRIES" ]]; then
    echo "buttons-entrypoint: ERROR: ollama unreachable at $OLLAMA after $((TRIES * WAIT_SECS))s; refusing to serve an empty index" >&2
    exit 1
  fi
  sleep "$WAIT_SECS"
done

if ! "$VENV" "$APP_ROOT/deep-agent/build_index.py"; then
  echo "buttons-entrypoint: ERROR: index build failed; refusing to serve an empty index" >&2
  exit 1
fi
exec "$@"
