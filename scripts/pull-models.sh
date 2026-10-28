#!/usr/bin/env bash
# Pull the Ollama models this box serves. Re-runnable; skips models already present.
# Usage: ./pull-models.sh [model ...]   (defaults to $OLLAMA_MODELS from .env)
set -euo pipefail

# Compose loads .env for interpolation, but a bare host script does not —
# source it so OLLAMA_MODELS from .env works when no args are given.
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

MODELS="${*:-${OLLAMA_MODELS:-}}"
if [[ -z "$MODELS" ]]; then
  echo "no models specified: pass model names or set OLLAMA_MODELS" >&2
  exit 1
fi

for m in $MODELS; do
  # docker compose exec resolves the service name; plain `docker exec ollama`
  # fails because the real container is <project>-ollama-1 (remote-ollama-1).
  if docker compose exec ollama ollama list 2>/dev/null | grep -q "^$m\b"; then
    echo "already present: $m"
  else
    echo "pulling: $m"
    docker compose exec ollama ollama pull "$m"
  fi
done
echo "done"
