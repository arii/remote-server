#!/usr/bin/env bash
# Parse KEY=VALUE lines without executing the file as shell.
# Usage: load_env_file <file> [VAR...] (only named vars are exported)
load_env_file() {
  local file="${1:-}"
  if [ -z "$file" ]; then echo "load_env_file: missing file argument" >&2; return 1; fi
  [ -f "$file" ] || return 0
  shift
  local line name value want
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    line="${line#"${line%%[![:space:]]*}"}"
    case "$line" in ''|'#'*) continue ;; esac
    case "$line" in *=*) ;; *) continue ;; esac
    name="${line%%=*}"
    value="${line#*=}"
    case "$name" in [A-Za-z_]*) ;; *) continue ;; esac
    case "$name" in *[!A-Za-z0-9_]* | '') continue ;; esac
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    case "$value" in '"'*) case "$value" in *'"') value="${value#\"}"; value="${value%\"}" ;; esac ;; esac
    case "$value" in "'"*"'") value="${value#\'}"; value="${value%\'}" ;; esac
    if [ "$#" -gt 0 ]; then
      for want in "$@"; do
        if [ "$want" = "$name" ]; then declare -gx "$name=$value"; break; fi
      done
    else
      declare -gx "$name=$value"
    fi
  done < "$file"
}
