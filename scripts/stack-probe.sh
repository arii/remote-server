#!/bin/sh
# stack-probe.sh — hourly S5 health probe (issues #1021/#1025 loop).
#
# Checks, in order: Selenium Grid session load, buttons /health, RAG
# query path, Ollama models + generation, Nebius fallback resolution.
# Prints one PASS/FAIL line per check; exit 0 only when all pass.
# No dependencies beyond curl, python3, and the repo's `td` CLI.
#
# Usage: sh scripts/stack-probe.sh
set -u
pass=0
fail=0
report() {
  if [ "$1" = "0" ]; then
    echo "PASS: $2"
    pass=$((pass + 1))
  else
    echo "FAIL: $2"
    fail=$((fail + 1))
  fi
}

grid=$(curl -s -m 10 http://browser:4444/status 2>/dev/null || true)
echo "$grid" | grep -q '"ready"' 2>/dev/null
report "$?" "grid status reachable (ready flag present)"

curl -s -m 10 -o /dev/null -w "%{http_code}" http://buttons:8000/health 2>/dev/null | grep -q 200
report "$?" "buttons /health returns 200"

# Canary: exit code alone cannot catch a stale/empty index (query
# succeeds with zero hits, as boomtick's did 2026-11-08). Require hits.
td rag query --query "oauth2 proxy github" --json 2>/dev/null | python3 -c \
  "import json,sys; sys.exit(0 if json.load(sys.stdin).get('count',0) > 0 else 1)" >/dev/null 2>&1
report "$?" "td rag query returns hits (index non-empty)"

curl -s -m 10 http://ollama:11434/api/tags 2>/dev/null | grep -q '"models"'
report "$?" "ollama /api/tags lists models"

curl -s -m 120 -X POST http://ollama:11434/api/generate \
  -d '{"model":"qwen3:8b","prompt":"ok","stream":false}' 2>/dev/null | grep -q '"response"'
report "$?" "ollama generate returns a response"

python3 -c "import sys; sys.path.insert(0, 'deep-agent'); from model_router import resolve_llm; print(resolve_llm(prefer='nebius'))" >/dev/null 2>&1
report "$?" "nebius fallback resolves via resolve_llm(prefer=nebius)"

echo "---"
echo "probe: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
