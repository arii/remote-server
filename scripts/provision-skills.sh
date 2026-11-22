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
# verify-skills.sh --check-upstream proves all 25 hashes match upstream
# HEAD. Our lock pins raw SKILL.md SHA-256 (written by `muse skills`);
# the npx tool uses incompatible lock semantics and would rewrite it.
#
# swarm is intentionally NOT in this lock: it is a project skill now
# (tracked at .agents/skills/swarm/SKILL.md with the quickjs-missing
# fallback), reviewed via PRs like lane-coder — provisioning must never
# overwrite the tracked copy with upstream (#1176).
#
# Sources: public GitHub repos plus arii/skills (PRIVATE, owner Ari, standard
# layout skills/<name>/SKILL.md). Fetches use plain HTTPS git, so private
# access comes from the caller's git credentials (the sandbox gateway helper
# or a developer's credential helper); this script adds no auth of its own.
# Without access to a private source the entry fails (or warns with
# --fail-open) and the rest still provision.
#
# Tracked project skills (e.g. lane-coder, swarm, and locked ones such as
# td-tools, lane-orchestrator, independent-tests) are skipped: their
# .agents/skills/<name> is git-tracked and is never overwritten (#1176).
# Their lock pins record the upstream copy and are checked by
# verify-skills.sh --check-upstream.
#
# Bumping a pin: resolve the new source commit, provision it, then
#   bash scripts/verify-skills.sh --repin   (npm run skills:repin)
# update "ref" in skills-lock.json to the full 40-char commit, review the
# diff and commit the lock. CI (--check-upstream) skips arii/skills because
# its default token cannot read it; run it locally with access.
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

# "name|source|skillPath|computedHash|ref" per lock entry.
lock_entries() {
  python3 - "$LOCK" <<'EOF'
import json, sys
with open(sys.argv[1]) as f:
    lock = json.load(f)
for name, e in lock["skills"].items():
    print(f"{name}|{e['source']}|{e['skillPath']}|{e['computedHash']}|{e.get('ref', '')}")
EOF
}

sha256_file() { sha256sum "$1" | cut -d' ' -f1; }

installed=0 skipped=0
while IFS='|' read -r name source skill_path want ref; do
  dest="$SKILLS_DIR/$name/SKILL.md"
  if git -C "$ROOT" ls-files --error-unmatch -- ".agents/skills/$name/SKILL.md" >/dev/null 2>&1; then
    echo "provision-skills: $name is a git-tracked project skill, not overwriting" >&2
    skipped=$((skipped + 1))
    continue
  fi
  if [[ -f "$dest" ]] && [[ "$(sha256_file "$dest")" == "$want" ]]; then
    skipped=$((skipped + 1))
    continue
  fi
  # (Re)install: shallow-clone or fetch pinned ref from the source, copy the skill dir.
  skill_dir="$(dirname "$skill_path")"
  clone_dir="$WORK_DIR/$(echo "$source" | tr '/:' '__')"
  if [[ ! -d "$clone_dir" ]]; then
    if [[ -n "$ref" ]]; then
      git init -q "$clone_dir"
      git -C "$clone_dir" remote add origin "https://github.com/${source}.git"
      git -C "$clone_dir" fetch -q --depth 1 origin "$ref" || { fail "fetch $ref failed for $source"; continue; }
      git -C "$clone_dir" checkout -q "$ref" || { fail "checkout $ref failed for $source"; continue; }
    else
      git clone -q --depth 1 "https://github.com/${source}.git" "$clone_dir" \
        || { fail "clone failed for $source"; continue; }
    fi
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

if [[ -f "$ROOT/scripts/setup-agent-skills.sh" ]]; then
  bash "$ROOT/scripts/setup-agent-skills.sh" || true
fi
