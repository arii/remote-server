#!/usr/bin/env bash
set -euo pipefail

# Script to canonically configure permissions in Antigravity settings:
# - Boomtick & Browser / Chrome DevTools MCP commands
# - Common agent and developer CLI tools (git, gh, archil, pytest, python, venv, etc.)
# - Common CLI utility commands (echo, head, tail, stat, file, touch, sed, etc.)
# Both exact command and wildcard argument forms 'command(<tool> *)' are registered
# so that subcommands and flags never trigger repeated manual confirmation prompts.

SETTINGS_FILE="${SETTINGS_FILE:-${HOME}/.gemini/antigravity-cli/settings.json}"
CONFIG_FILE="${CONFIG_FILE:-${HOME}/.gemini/config/config.json}"
export SETTINGS_FILE CONFIG_FILE

echo "==> Updating Antigravity permissions..."

python3 - <<'EOF'
import json
import os
import shutil
from datetime import datetime

# Tool names that will have both "command(<tool>)" and "command(<tool> *)" added
CLI_TOOLS = [
    # Developer & agent execution tooling
    "git",
    "gh",
    "archil",
    "docker",
    "docker-compose",
    "pnpm",
    "npm",
    "node",
    "npx",
    "yarn",
    "corepack",
    "fallow",
    "pytest",
    "python",
    "python3",
    "python3.10",
    "python3.11",
    "python3.12",
    "pip",
    "pip3",
    "py",
    "uv",
    "td",
    "curl",
    "wget",
    "bash",
    "sh",
    "make",
    "go",
    "cargo",
    "shellcheck",
    "oxlint",
    "eslint",
    "openssl",
    "ssh-keyscan",
    "playwright-cli",
    "boomtick",
    "boomtick-mcp",

    # Remote copy / shell
    "ssh",
    "scp",
    "rsync",

    # Inspection / utility commands
    "echo",
    "head",
    "tail",
    "stat",
    "file",
    "touch",
    "sed",
    "cat",
    "ls",
    "grep",
    "find",
    "awk",
    "diff",
    "wc",
    "sort",
    "uniq",
    "cut",
    "tr",
    "which",
    "basename",
    "dirname",
    "less",
    "more",
    "nl",
    "tee",
    "xargs",
    "jq",
    "rg",
    "patch",
    "cmp",
    "base64",
    "sha256sum",
    "md5sum",

    # File operations
    "cp",
    "mv",
    "mkdir",
    "rmdir",
    "ln",
    "tar",
    "gzip",
    "gunzip",
    "zip",
    "unzip",
    "chmod",
    "chown",

    # System inspection & cleanup
    "ps",
    "df",
    "du",
    "free",
    "uptime",
    "lscpu",
    "date",
    "hostname",
    "uname",
    "id",
    "whoami",
    "printenv",
    "env",
    "lsblk",
    "ss",
    "journalctl",
    "fuser",
    "kill",
    "killall",
    "pkill",
]

# Specific paths, patterns, and MCP wildcards
EXPLICIT_RULES = [
    # Boomtick MCP server wildcards
    "mcp(boomtick/*)",
    "mcp(boomtick-mcp/*)",

    # Browser & DevTools MCP server wildcards
    "mcp(browser-mcp/*)",
    "mcp(browsermcp/*)",
    "mcp(browser/*)",
    "mcp(chrome-devtools/*)",

    # Virtual environment binaries (both relative and path wildcards)
    "command(.venv/bin/*)",
    "command(.venv/bin/python *)",
    "command(.venv/bin/python)",
    "command(.venv/bin/python3 *)",
    "command(.venv/bin/python3)",
    "command(.venv/bin/pytest *)",
    "command(.venv/bin/pytest)",
    "command(./.venv/bin/*)",
    "command(./.venv/bin/python *)",
    "command(./.venv/bin/python)",
    "command(./.venv/bin/python3 *)",
    "command(./.venv/bin/python3)",
    "command(./.venv/bin/pytest *)",
    "command(./.venv/bin/pytest)",

    # Common docker compose forms
    "command(docker compose)",
    "command(docker compose *)",

    # Impeccable script binary
    "command(./.agents/skills/impeccable/scripts/bin/linux-x64/impeccable)",
    "command(./.agents/skills/impeccable/scripts/bin/linux-x64/impeccable *)",
]

# Generate composite rule list
RULES_TO_ALLOW = list(EXPLICIT_RULES)
for tool in CLI_TOOLS:
    base_rule = f"command({tool})"
    wildcard_rule = f"command({tool} *)"
    if base_rule not in RULES_TO_ALLOW:
        RULES_TO_ALLOW.append(base_rule)
    if wildcard_rule not in RULES_TO_ALLOW:
        RULES_TO_ALLOW.append(wildcard_rule)

# Deliberately NOT allowlisted (stay prompt-gated):
# sudo/su/passwd (privilege boundary), shutdown/reboot (availability),
# dd/mkfs/fdisk/parted (disk destruction), systemctl/service (host control),
# crontab (silent persistence), mount, iptables/nft/ufw.

def update_cli_settings(filepath):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    data = {}
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            backup_path = f"{filepath}.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            shutil.copy2(filepath, backup_path)
            print(f"Created backup at: {backup_path}")
        except Exception as e:
            print(f"Warning: Could not read existing {filepath} ({e}), initializing fresh configuration.")

    perms = data.setdefault("permissions", {})
    allows = perms.setdefault("allow", [])

    added_count = 0
    for rule in RULES_TO_ALLOW:
        if rule not in allows:
            allows.append(rule)
            added_count += 1

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")

    print(f"Updated {filepath}: added {added_count} new rule(s) (total allowed: {len(allows)}).")

def update_global_config(filepath):
    if not os.path.exists(filepath):
        return
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return

    user_settings = data.setdefault("userSettings", {})
    grants = user_settings.setdefault("globalPermissionGrants", {})
    allows = grants.setdefault("allow", [])

    added = 0
    for rule in RULES_TO_ALLOW:
        if rule not in allows:
            allows.append(rule)
            added += 1

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")

    print(f"Updated global grants in {filepath}: added {added} rule(s).")

cli_path = os.getenv("SETTINGS_FILE", os.path.expanduser("~/.gemini/antigravity-cli/settings.json"))
config_path = os.getenv("CONFIG_FILE", os.path.expanduser("~/.gemini/config/config.json"))

update_cli_settings(cli_path)
update_global_config(config_path)

print(f"\nSuccessfully configured {len(RULES_TO_ALLOW)} permissions.")
EOF

echo ""
echo "Done! Antigravity permissions updated."
