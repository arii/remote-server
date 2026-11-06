#!/usr/bin/env bash
# Verify vendored skills against skills-lock.json (issue #557).
#
# skills-lock.json pins the sha256 of every third-party skill's installed
# SKILL.md, but `muse skills` (1.4.14) implements no lock check — the binary
# contains no reference to skills-lock/computedHash — so without this script
# the pins have zero consumers. This verifier is the enforcement path.
#
#   verify (default)  every locked skill is installed at
#                     .agents/skills/<name>/SKILL.md with a matching hash;
#                     any extra untracked skill dir fails (git-tracked project
#                     skills such as lane-coder are reviewed via PRs instead).
#   --repin           rewrite computedHash values from the installed files
#                     (the bump procedure: reinstall/upgrade, review the diff,
#                     re-pin, commit the lock).
#   --check-upstream  clone each locked source repo shallowly and prove the
#                     lock matches upstream (the CI path: fresh checkouts have
#                     no .agents/skills to verify against).
#
# Limitation: the lock pins SKILL.md only. Sibling files shipped inside a
# skill dir (references/, scripts/) are not hash-checked.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK="$ROOT/skills-lock.json"
SKILLS_DIR="$ROOT/.agents/skills"
MODE="verify"
WORK_DIR=""
trap '[ -n "$WORK_DIR" ] && rm -rf "$WORK_DIR"' EXIT

usage() {
  cat <<'EOF'
usage: verify-skills.sh [--lock FILE] [--skills-dir DIR] [--repin | --check-upstream]

  (default)       verify installed skills against the lock
  --repin         update lock hashes from installed files (bump procedure)
  --check-upstream  verify the lock matches upstream sources (needs network)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --lock) LOCK="$2"; shift 2 ;;
    --skills-dir) SKILLS_DIR="$2"; shift 2 ;;
    --repin) MODE="repin"; shift ;;
    --check-upstream) MODE="upstream"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "error: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

# Print "name|source|skillPath|computedHash" lines from the lock.
lock_entries() {
  python3 - "$LOCK" <<'EOF'
import json, sys
with open(sys.argv[1]) as f:
    lock = json.load(f)
for name, e in lock["skills"].items():
    print(f"{name}|{e['source']}|{e['skillPath']}|{e['computedHash']}")
EOF
}

sha256_file() {
  sha256sum "$1" | cut -d' ' -f1
}

# Zero when the skill is a git-tracked project skill (reviewed via PRs).
is_tracked_skill() {
  git -C "$ROOT" ls-files --error-unmatch -- ".agents/skills/$1/SKILL.md" >/dev/null 2>&1
}

do_verify() {
  local failures=0 checked=0
  while IFS='|' read -r name _source _path want; do
    checked=$((checked + 1))
    local file="$SKILLS_DIR/$name/SKILL.md"
    if [[ ! -f "$file" ]]; then
      echo "MISSING  $name ($file not installed — fresh worktree? run bash scripts/provision-skills.sh first)" >&2
      failures=$((failures + 1))
      continue
    fi
    local got
    got="$(sha256_file "$file")"
    if [[ "$got" != "$want" ]]; then
      echo "MISMATCH $name (installed file does not match lock)" >&2
      failures=$((failures + 1))
    fi
  done < <(lock_entries)
  if [[ -d "$SKILLS_DIR" ]]; then
    local locked
    locked="$(lock_entries | cut -d'|' -f1)"
    while IFS= read -r dir; do
      local name
      name="$(basename "$dir")"
      if grep -qx "$name" <<<"$locked"; then
        continue
      fi
      if is_tracked_skill "$name"; then
        echo "TRACKED  $name (project skill, reviewed via git)" >&2
        continue
      fi
      echo "UNKNOWN  $name (not in lock, not tracked — remove or pin it)" >&2
      failures=$((failures + 1))
    done < <(find "$SKILLS_DIR" -mindepth 1 -maxdepth 1 -type d)
  fi
  if [[ "$failures" -gt 0 ]]; then
    echo "verify-skills: $failures problem(s) across $checked locked skill(s)" >&2
    return 1
  fi
  echo "verify-skills: OK ($checked locked skill(s) match)"
}

do_repin() {
  local failures=0 repinned=0
  while IFS='|' read -r name _source _path _want; do
    local file="$SKILLS_DIR/$name/SKILL.md"
    if [[ ! -f "$file" ]]; then
      echo "MISSING  $name (cannot pin what is not installed)" >&2
      failures=$((failures + 1))
      continue
    fi
    local got
    got="$(sha256_file "$file")"
    python3 - "$LOCK" "$name" "$got" <<'EOF'
import json, sys
path, name, got = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as f:
    lock = json.load(f)
lock["skills"][name]["computedHash"] = got
with open(path, "w") as f:
    json.dump(lock, f, indent=2)
    f.write("\n")
EOF
    echo "PINNED   $name $got" >&2
    repinned=$((repinned + 1))
  done < <(lock_entries)
  if [[ "$failures" -gt 0 ]]; then
    echo "verify-skills: re-pin incomplete ($failures missing)" >&2
    return 1
  fi
  echo "verify-skills: re-pinned $repinned skill(s) — review the diff, then commit the lock"
}

do_upstream() {
  local failures=0 checked=0
  WORK_DIR="$(mktemp -d)"
  local n=0
  declare -A cloned=()
  while IFS='|' read -r name source path want; do
    checked=$((checked + 1))
    if [[ -z "${cloned[$source]:-}" ]]; then
      n=$((n + 1))
      git clone -q --depth 1 "https://github.com/$source" "$WORK_DIR/repo-$n" >&2
      cloned[$source]="$WORK_DIR/repo-$n"
    fi
    local file="${cloned[$source]}/$path"
    if [[ ! -f "$file" ]]; then
      echo "NO-UPSTREAM-PATH $name ($source:$path)" >&2
      failures=$((failures + 1))
      continue
    fi
    local got
    got="$(sha256_file "$file")"
    if [[ "$got" != "$want" ]]; then
      echo "UPSTREAM-MISMATCH $name (lock does not match $source default branch)" >&2
      failures=$((failures + 1))
    fi
  done < <(lock_entries)
  if [[ "$failures" -gt 0 ]]; then
    echo "verify-skills: lock matches upstream for $((checked - failures))/$checked skill(s)" >&2
    return 1
  fi
  echo "verify-skills: lock matches upstream for all $checked skill(s)"
}

case "$MODE" in
  verify) do_verify ;;
  repin) do_repin ;;
  upstream) do_upstream ;;
esac
