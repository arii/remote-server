#!/usr/bin/env bash
# Pull the Ollama models this box serves and configure custom 16k Modelfiles.
# Re-runnable; skips models already present unless tuning is required.
# Usage: ./pull-models.sh [model ...]   (defaults to $OLLAMA_MODELS from .env)
set -euo pipefail

# Parse .env without executing it (no Compose interpolation for bare host scripts).
# shellcheck source=scripts/lib/load_env.sh
. scripts/lib/load_env.sh
load_env_file .env OLLAMA_MODELS OLLAMA_NUM_CTX OLLAMA_TEMPERATURE

MODELS="${*:-${OLLAMA_MODELS:-}}"
if [[ -z "$MODELS" ]]; then
  echo "no models specified: pass model names or set OLLAMA_MODELS" >&2
  exit 1
fi

OLLAMA_NUM_CTX="${OLLAMA_NUM_CTX:-16384}"
OLLAMA_TEMPERATURE="${OLLAMA_TEMPERATURE:-0.2}"

read -ra MODEL_LIST <<< "$MODELS"
for m in "${MODEL_LIST[@]}"; do
  # Determine base model and whether custom Modelfile tuning applies
  base_model="$m"
  target_model="$m"

  # If model is requested with a suffix like :tag-16k, derive base tag
  if [[ "$m" =~ ^(.*):([0-9a-zA-Z._-]+)-16k$ ]]; then
    base_model="${BASH_REMATCH[1]}:${BASH_REMATCH[2]}"
    target_model="$m"
  fi

  # Pull base model tag if not present
  if docker compose exec ollama ollama list 2>/dev/null | grep -q "^$base_model\b"; then
    echo "base model already present: $base_model"
  else
    echo "pulling base model: $base_model"
    docker compose exec ollama ollama pull "$base_model"
  fi

  # Configure tuned 16k Modelfile parameters (num_ctx 16384, temperature 0.2)
  # Applies to coder models, explicitly tagged -16k models, or all configured models
  echo "configuring 16k context window for $target_model (num_ctx=$OLLAMA_NUM_CTX, temperature=$OLLAMA_TEMPERATURE)..."
  modelfile_content=$(printf "FROM %s\nPARAMETER num_ctx %s\nPARAMETER temperature %s\n" "$base_model" "$OLLAMA_NUM_CTX" "$OLLAMA_TEMPERATURE")
  echo "$modelfile_content" | docker compose exec -i ollama ollama create "$target_model" -f -
done
echo "done"
