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
# SKIP_INDEX_BUILD=1 (one-off shells via `docker compose run`),
# CHUNK_FLOOR (min embedding rows for a servable index, default 1000:
# healthy builds hold ~3700, the stale #1119 build held 311).
set -euo pipefail

APP_ROOT="${APP_ROOT:-/app}"
INDEX_DIR="${S5_CHROMA_DIR:-$APP_ROOT/deep-agent/chroma_db}"
STACK="${BUTTONS_STACK:-$APP_ROOT}"
SQLITE="$INDEX_DIR/chroma.sqlite3"
TD_META="$STACK/.td_rag/meta.json"
FLOOR="${CHUNK_FLOOR:-1000}"
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

# Rebuild policy (#1119): a non-empty sqlite is not proof of a
# servable index (stale thin builds serve zero hits while /health
# says ready). Skip only when the sqlite holds at least FLOOR
# embedding rows AND, for git checkouts, td's recorded SHA matches
# HEAD. Anything else — missing/thin sqlite, missing meta, SHA
# behind — rebuilds. Non-git stacks skip on thickness alone (no
# HEAD to compare against).
_chunks() {
  python3 -c 'import pathlib,sqlite3,sys
try:
    uri = pathlib.Path(sys.argv[1]).resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    print(con.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0])
except Exception:
    pass' "$SQLITE" 2>/dev/null
}
_stack_head() {
  git -C "$STACK" rev-parse HEAD 2>/dev/null || true
}
_meta_sha() {
  python3 -c 'import json,sys
try:
    print(json.load(open(sys.argv[1])).get("last_indexed_sha", ""))
except Exception:
    pass' "$TD_META" 2>/dev/null
}
_skip_build() {
  [[ -s "$SQLITE" ]] || return 1
  local chunks head sha
  chunks=$(_chunks)
  [[ "$chunks" =~ ^[0-9]+$ ]] && (( chunks >= FLOOR )) || return 1
  head=$(_stack_head)
  [[ -z "$head" ]] && return 0
  sha=$(_meta_sha)
  [[ -n "$sha" && "$sha" == "$head" ]] || return 1
  return 0
}

if [[ ! -e "$BUILDING" ]] && _skip_build; then
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
  # in the Dockerfile). --full: this path runs when the index is
  # missing, thin, or behind HEAD, so incremental state (stale SHA
  # with wiped artifacts, changed embedding backend) must not
  # no-op the build.
  if ! td rag index --repo-root "${BUTTONS_STACK:-$APP_ROOT}" --full; then
    echo "buttons-entrypoint: ERROR: index build failed; queries degrade until the index is rebuilt" >&2
    echo "index build failed (see $LOG)" > "$FAILED"
    rm -f "$BUILDING"
    exit 1
  fi
  # #1094: a zero-exit build with no embedding backend writes only
  # meta (no sqlite). The server reads INDEX_DIR, so a missing or
  # empty sqlite here is a failed build, not a success — record it
  # loudly instead of serving missing forever.
  if [[ ! -s "$INDEX_DIR/chroma.sqlite3" ]]; then
    echo "buttons-entrypoint: ERROR: index build produced no sqlite at $INDEX_DIR/chroma.sqlite3; queries degrade until the index is rebuilt" >&2
    echo "index build produced no sqlite at $INDEX_DIR/chroma.sqlite3 (see $LOG)" > "$FAILED"
    rm -f "$BUILDING"
    exit 1
  fi
  # #1119: a fresh build must also prove thickness — a thin sqlite
  # serves zero hits while looking healthy. Record what this build
  # proved in build_meta.json (SHA + chunk count) for the next boot
  # and for operators.
  CHUNKS=$(_chunks)
  if ! [[ "$CHUNKS" =~ ^[0-9]+$ ]] || (( CHUNKS < FLOOR )); then
    echo "buttons-entrypoint: ERROR: index build produced only ${CHUNKS:-unreadable} chunks (floor $FLOOR); queries degrade until the index is rebuilt" >&2
    echo "index build produced only ${CHUNKS:-unreadable} chunks at $INDEX_DIR/chroma.sqlite3 (floor $FLOOR, see $LOG)" > "$FAILED"
    rm -f "$BUILDING"
    exit 1
  fi
  python3 - "$INDEX_DIR/build_meta.json" "$(_stack_head)" "$CHUNKS" "$FLOOR" <<'EOF'
import datetime, json, sys
out, sha, chunks, floor = (sys.argv[1], sys.argv[2],
                           int(sys.argv[3]), int(sys.argv[4]))
json.dump({"sha": sha or None, "chunks": chunks, "floor": floor,
           "built_at": datetime.datetime.now(
               datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
          open(out, "w"), indent=2)
EOF
  rm -f "$BUILDING"
  echo "buttons-entrypoint: background index build done"
) >> "$LOG" 2>&1 &
disown 2> /dev/null || true

echo "buttons-entrypoint: server starting now; RAG index building in background (log: $LOG)"
exec "$@"
