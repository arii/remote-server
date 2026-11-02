#!/usr/bin/env bash
set -euo pipefail

# Script to canonically configure permissions in Antigravity settings:
# - Boomtick MCP commands
# - Browser MCP commands
# - Common agent and developer CLI tools (git, gh, docker, pnpm, fallow, pytest, etc.)
# - Common CLI utility commands (echo, head, tail, stat, file, touch, sed, etc.)

SETTINGS_FILE="${SETTINGS_FILE:-${HOME}/.gemini/antigravity-cli/settings.json}"
CONFIG_FILE="${CONFIG_FILE:-${HOME}/.gemini/config/config.json}"
export SETTINGS_FILE CONFIG_FILE

echo "==> Updating Antigravity permissions..."

python3 - <<'EOF'
import json
import os
import shutil
from datetime import datetime

RULES_TO_ALLOW = [
    # Boomtick MCP server wildcard
    "mcp(boomtick/*)",

    # Browser MCP server wildcards
    "mcp(browser-mcp/*)",
    "mcp(browsermcp/*)",
    "mcp(browser/*)",

    # Developer & agent execution tooling
    "command(git)",
    "command(gh)",
    "command(docker)",
    "command(pnpm)",
    "command(npm)",
    "command(node)",
    "command(npx)",
    "command(yarn)",
    "command(corepack)",
    "command(fallow)",
    "command(pytest)",
    "command(python)",
    "command(python3)",
    "command(pip)",
    "command(pip3)",
    "command(uv)",
    "command(td)",
    "command(curl)",
    "command(wget)",
    "command(bash)",
    "command(sh)",
    "command(make)",
    "command(go)",
    "command(cargo)",
    "command(shellcheck)",
    "command(openssl)",
    "command(ssh-keyscan)",

    # Remote copy / shell (routine deploy and checkout sync)
    "command(ssh)",
    "command(scp)",
    "command(rsync)",

    # User-requested common CLI inspection/utility commands
    "command(echo)",
    "command(head)",
    "command(tail)",
    "command(stat)",
    "command(file)",
    "command(touch)",
    "command(sed)",

    # Standard complementary CLI utilities
    "command(cat)",
    "command(ls)",
    "command(grep)",
    "command(find)",
    "command(awk)",
    "command(diff)",
    "command(wc)",
    "command(sort)",
    "command(uniq)",
    "command(cut)",
    "command(tr)",
    "command(which)",
    "command(basename)",
    "command(dirname)",
    "command(less)",
    "command(more)",
    "command(nl)",
    "command(tee)",
    "command(xargs)",
    "command(jq)",
    "command(rg)",
    "command(patch)",
    "command(cmp)",
    "command(base64)",
    "command(sha256sum)",
    "command(md5sum)",

    # File operations (routine workspace and build-dir management)
    "command(cp)",
    "command(mv)",
    "command(mkdir)",
    "command(rmdir)",
    "command(ln)",
    "command(tar)",
    "command(gzip)",
    "command(gunzip)",
    "command(zip)",
    "command(unzip)",
    "command(chmod)",
    "command(chown)",

    # System inspection and stale-process cleanup
    "command(ps)",
    "command(df)",
    "command(du)",
    "command(free)",
    "command(uptime)",
    "command(date)",
    "command(hostname)",
    "command(uname)",
    "command(id)",
    "command(whoami)",
    "command(printenv)",
    "command(env)",
    "command(lsblk)",
    "command(ss)",
    "command(journalctl)",
    "command(fuser)",
    "command(kill)",
    "command(killall)",
    "command(pkill)",
]

# Deliberately NOT allowlisted (stay prompt-gated): rm (destructive),
# sudo/su/passwd (privilege boundary), shutdown/reboot (availability),
# dd/mkfs/fdisk/parted (disk destruction), systemctl/service (host
# control), crontab (silent persistence), mount, iptables/nft/ufw.

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

print("\nRules allowed:")
for r in RULES_TO_ALLOW:
    print(f"  + {r}")
EOF

echo ""
echo "Done! Restart your Antigravity CLI session (or run /permissions in chat) to verify."
