#!/usr/bin/env bash
# Install vendored skills from skills-lock.json (fresh-install path).
#
# .agents/skills/ is deliberately untracked (per-machine, never committed).
# On a fresh checkout or reinstall it does not exist; this script recreates
# it from the lock: shallow-clone each locked source repo, copy the skill
# dir into place, then run verify-skills.sh to prove hashes match.
#
# NOTE: this deliberately does NOT use `npx skills experimental_install`.
# That tool computes directory digests and refuses our lock ("swarm has
# changed since skills-lock.json was written") even though
# verify-skills.sh --check-upstream proves all 26 hashes match upstream
# HEAD. Our lock pins raw SKILL.md SHA-256 (written by `muse skills`);
# the npx tool uses incompatible lock semantics and would rewrite it.
#
#   provision-skills.sh [--fail-open]
#
# --fail-open  warn instead of exiting nonzero (for container bring-up:
#              skills must never block the stack from starting).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK="$ROOT/skills-lock.json"
SKILLS_DIR="$ROOT/.agents/skills"
FAIL_OPEN=0

if [[ "${1:-}" == "--fail-open" ]]; then FAIL_OPEN=1; fi

fail() {
  echo "provision-skills: $1" >&2
  if [[ "$FAIL_OPEN" == "1" ]]; then
    echo "provision-skills: continuing (--fail-open)" >&2
  else
    exit 1
  fi
}

command -v git >/dev/null || { fail "git not found"; exit 0; }
command -v python3 >/dev/null || { fail "python3 not found"; exit 0; }

mkdir -p "$SKILLS_DIR"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

# "name|source|skillPath|computedHash" per lock entry.
lock_entries() {
  python3 - "$LOCK" <<'EOF'
import json, sys
with open(sys.argv[1]) as f:
    lock = json.load(f)
for name, e in lock["skills"].items():
    print(f"{name}|{e['source']}|{e['skillPath']}|{e['computedHash']}")
EOF
}

sha256_file() { sha256sum "$1" | cut -d' ' -f1; }

installed=0 skipped=0
while IFS='|' read -r name source skill_path want; do
  dest="$SKILLS_DIR/$name/SKILL.md"
  if [[ -f "$dest" ]] && [[ "$(sha256_file "$dest")" == "$want" ]]; then
    skipped=$((skipped + 1))
    continue
  fi
  # (Re)install: shallow-clone the source, copy the skill dir.
  skill_dir="$(dirname "$skill_path")"
  clone_dir="$WORK_DIR/$(echo "$source" | tr '/:' '__')"
  if [[ ! -d "$clone_dir" ]]; then
    git clone --depth 1 "https://github.com/${source}.git" "$clone_dir" \
      || { fail "clone failed for $source"; continue; }
  fi
  if [[ ! -f "$clone_dir/$skill_path" ]]; then
    fail "$source has no $skill_path"
    continue
  fi
  rm -rf "${SKILLS_DIR:?}/${name:?}"
  mkdir -p "$SKILLS_DIR/$name"
  cp -r "$clone_dir/$skill_dir/." "$SKILLS_DIR/$name/"
  installed=$((installed + 1))
done < <(lock_entries)

echo "provision-skills: $installed installed, $skipped already satisfied"

if ! bash "$ROOT/scripts/verify-skills.sh"; then
  fail "post-install verification failed"
fi
