#!/usr/bin/env bash
# No shadow skill copies (issue #1207; .github/skills added #1214/#1215).
#
# .agents/skills/ is the single source of truth: provisioned from
# skills-lock.json, or tracked project skills reviewed via PR.
# .claude/skills/ and .github/skills/ exist only because some consumers
# read those paths, so every entry under each shim dir must be a
# symlink into the canonical tree — a real copy drifts (the loader
# already skips one id as shadowed). Dangling links pass: fresh
# checkouts provision .agents/skills/ via scripts/provision-skills.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHIM_DIRS=("$ROOT/.claude/skills" "$ROOT/.github/skills")
fail=0

for SHIM_DIR in "${SHIM_DIRS[@]}"; do
  [[ -d "$SHIM_DIR" ]] || continue
  while IFS= read -r entry; do
    if [[ ! -L "$entry" ]]; then
      echo "shadow skill copy: $entry is a real file/dir, not a symlink" >&2
      fail=1
    fi
  done < <(find "$SHIM_DIR" -mindepth 1 -maxdepth 1)
done

exit "$fail"
