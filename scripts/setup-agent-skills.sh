#!/usr/bin/env bash
# Provision universal agent-level skills from the private hello repo
# (/srv/repos/hello/.agents/skills) to CLI agent engines:
# - Antigravity CLI (AGY): /root/.gemini/config/skills/
# - Claude / Muse: /root/.claude/skills/
# - Copilot: /root/.copilot/skills/
#
# Prevents duplicate revision control across repos and survives reboots.
set -euo pipefail

SOURCE_DIR="${HELLO_SKILLS_DIR:-/srv/repos/hello/.agents/skills}"

if [[ ! -d "$SOURCE_DIR" ]]; then
  # Fail-open if hello repo is not mounted or present
  exit 0
fi

TARGET_DIRS=(
  "/root/.gemini/config/skills"
  "/root/.claude/skills"
  "/root/.copilot/skills"
)

linked=0
for target_dir in "${TARGET_DIRS[@]}"; do
  mkdir -p "$target_dir"
  for skill_path in "$SOURCE_DIR"/*; do
    if [[ -d "$skill_path" ]]; then
      skill_name="$(basename "$skill_path")"
      ln -sfn "$skill_path" "$target_dir/$skill_name"
      linked=$((linked + 1))
    fi
  done
done

echo "setup-agent-skills: $linked agent-level skill links verified from $SOURCE_DIR"
