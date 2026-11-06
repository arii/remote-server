#!/usr/bin/env bash
# buttons entrypoint: serve fast, build the RAG index in the background (#699).
#
# The image carries no chroma_db (dockerignored: a baked copy would be
# stale the moment docs drift). A fresh container execs the server
# immediately and builds the index as a supervised background step, so
# a deploy reaches /health in seconds instead of paying the ~10 min
# boot outage (#696 regression). Queries served while the build runs
# get a "warming, retry shortly" degraded message (never empty-silent
# answers), and /health reports the index state. A plain `restart`
# keeps the container filesystem, so the present-index fast path
# skips the rebuild (the index still matches the image's code).
#
# Readiness markers live in the index dir: .index_building while a
# build runs, .index_failed (with the reason) when the last build
# failed, index_build.log with the build transcript. A stale
# .index_building (container killed mid-build, then `restart`) forces
# a background rebuild — the on-disk sqlite may be partial.
#
# Env (all optional): APP_ROOT (/app),
# S5_CHROMA_DIR (index dir), OLLAMA_URL, OLLAMA_WAIT_SECS (5; sleep
# between Ollama readiness probes), OLLAMA_TRIES (36 probes),
# SKIP_INDEX_BUILD=1 (one-off shells via `docker compose run`).
set -euo pipefail

APP_ROOT="${APP_ROOT:-/app}"
INDEX_DIR="${S5_CHROMA_DIR:-$APP_ROOT/deep-agent/chroma_db}"
# Bare Ollama host: OLLAMA_URL ships with the /api/generate path in
# compose (repair-worker convention), so strip it before appending
# probe paths — same normalization as model_router._ollama_base
# (#968 fixed the identical suffix bug in the tags probe there).
OLLAMA="${OLLAMA_URL:-http://ollama:11434}"
OLLAMA="${OLLAMA%/}"
OLLAMA="${OLLAMA%/api/generate}"
OLLAMA="${OLLAMA%/}"
WAIT_SECS="${OLLAMA_WAIT_SECS:-5}"
TRIES="${OLLAMA_TRIES:-36}"
BUILDING="$INDEX_DIR/.index_building"
FAILED="$INDEX_DIR/.index_failed"
LOG="$INDEX_DIR/index_build.log"

if [[ "${SKIP_INDEX_BUILD:-}" == "1" ]]; then
  echo "buttons-entrypoint: SKIP_INDEX_BUILD=1, skipping index check"
  exec "$@"
fi

if [[ -s "$INDEX_DIR/chroma.sqlite3" && ! -e "$BUILDING" ]]; then
  echo "buttons-entrypoint: index present at $INDEX_DIR, skipping rebuild"
  rm -f "$FAILED"
  exec "$@"
fi

mkdir -p "$INDEX_DIR"
rm -f "$FAILED"
echo "started $(date -u +%FT%TZ)" > "$BUILDING"

# Supervised background build: wait for Ollama, then build. All
# output goes to the log file — never the container's stdio pipes,
# so exec'ing the server never blocks on a held-open fd. Markers flip
# .index_building -> gone on success, -> .index_failed on failure.
(
  echo "buttons-entrypoint: background index build starting ($INDEX_DIR)"
  i=0
  until curl -fsS --max-time 5 "$OLLAMA/api/tags" > /dev/null; do
    i=$((i + 1))
    if [[ "$i" -ge "$TRIES" ]]; then
      echo "buttons-entrypoint: ERROR: ollama unreachable at $OLLAMA after $((TRIES * WAIT_SECS))s" >&2
      echo "ollama unreachable at $OLLAMA after $((TRIES * WAIT_SECS))s" > "$FAILED"
      rm -f "$BUILDING"
      exit 1
    fi
    sleep "$WAIT_SECS"
  done
  # Index the mounted live checkout, not the baked copy: /app has
  # no .git and goes stale on every deploy (BUTTONS_STACK doctrine
  # in the Dockerfile).
  if ! td rag index --repo-root "${BUTTONS_STACK:-$APP_ROOT}"; then
    echo "buttons-entrypoint: ERROR: index build failed; queries degrade until the index is rebuilt" >&2
    echo "index build failed (see $LOG)" > "$FAILED"
    rm -f "$BUILDING"
    exit 1
  fi
  rm -f "$BUILDING"
  echo "buttons-entrypoint: background index build done"
) >> "$LOG" 2>&1 &
disown 2> /dev/null || true

echo "buttons-entrypoint: server starting now; RAG index building in background (log: $LOG)"
exec "$@"
