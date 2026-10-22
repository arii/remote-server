#!/usr/bin/env bash
# Pull the Ollama models this box serves. Re-runnable; skips models already present.
# Usage: ./pull-models.sh [model ...]   (defaults to $OLLAMA_MODELS)
set -euo pipefail

MODELS="${*:-${OLLAMA_MODELS:-}}"
if [[ -z "$MODELS" ]]; then
  echo "no models specified: pass model names or set OLLAMA_MODELS" >&2
  exit 1
fi

for m in $MODELS; do
  if docker exec ollama ollama list 2>/dev/null | grep -q "^$m\b"; then
    echo "already present: $m"
  else
    echo "pulling: $m"
    docker exec ollama ollama pull "$m"
  fi
done
echo "done"
