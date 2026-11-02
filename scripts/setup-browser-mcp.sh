#!/usr/bin/env bash
# Setup and configure Browser MCP (v2.3.0) and Antigravity directory structure.
# Idempotent — safe to re-run.
set -euo pipefail

TARGET_HOME="${TARGET_HOME:-${HOME}}"
GEMINI_DIR="${TARGET_HOME}/.gemini"
CONFIG_DIR="${GEMINI_DIR}/config"
RULES_DIR="${CONFIG_DIR}/rules"
AGY_CLI_DIR="${GEMINI_DIR}/antigravity-cli"
BIN_DIR="${AGY_CLI_DIR}/bin"
MCP_DIR="${AGY_CLI_DIR}/mcp"
BROWSER_MCP_DIR="${MCP_DIR}/browser-mcp"

echo "==> Setting up Browser MCP & Antigravity directory layout under ${GEMINI_DIR}..."

mkdir -p "${RULES_DIR}"
mkdir -p "${BIN_DIR}"
mkdir -p "${BROWSER_MCP_DIR}"

# 1. Launcher script start_browsermcp.sh
LAUNCHER="${BIN_DIR}/start_browsermcp.sh"
cat > "${LAUNCHER}" <<'EOF'
#!/bin/bash
# Persisted daemon home (survives terminal recreates, unlike ~/.browsermcp).
export BROWSER_MCP_HOME="${BROWSER_MCP_HOME:-/root/.local/share/browsermcp}"
exec npx -y @browsermcp/mcp@latest "$@"
EOF
chmod +x "${LAUNCHER}"
echo "Created executable launcher: ${LAUNCHER}"

# 2. Schema: browser.json
BROWSER_JSON="${BROWSER_MCP_DIR}/browser.json"
cat > "${BROWSER_JSON}" <<'EOF'
{
  "name": "browser",
  "description": "Controls the user's Chrome. Runs one command per line, stopping at the first error. Actions print one ✓ line, reads print their output. Do many steps per call.\ngoto <url> | back | forward | reload\nclick <target>\nfill <target> \"<text>\" [--submit]\ntype \"<text>\"\npress <key>\nselect <target> \"<value>\"\ncheck <target> | uncheck <target>\nwait <ms> | wait --text \"<text>\" | wait <target>\ntab list | tab new [<url>] | tab select <id> | tab close\nsnapshot [<target>] [-i] [--depth N]\nfind \"<text>\"\nget text|value|count|url|title [<target>]\nscreenshot [<target>]\neval \"<js function>\"\nMore (help <command>): scroll, hover, drag, upload, dialog, console, requests, request\nTargets: e12 (a snapshot ref), getByRole('button', { name: 'Go' }), getByLabel/getByText/getByPlaceholder('…'), or CSS.\nQuote text with spaces: \"like this\".",
  "parameters": {
    "properties": {
      "commands": {
        "description": "Command lines, one per line",
        "type": "string"
      }
    },
    "required": [
      "commands"
    ],
    "type": "object"
  }
}
EOF
echo "Created schema: ${BROWSER_JSON}"

# 3. Instructions: instructions.md
INSTRUCTIONS_MD="${BROWSER_MCP_DIR}/instructions.md"
cat > "${INSTRUCTIONS_MD}" <<'EOF'
Browser MCP controls the user's Chrome through the `browser` tool:
1. Act in batches: put every step you can predict in one call (targets like getByLabel('Email') work without a snapshot).
2. Look with `snapshot -i` or `find "<text>"`; read a whole page with `get text --max all`.
3. Re-snapshot after navigating: refs from an older page fail.
EOF
echo "Created instructions: ${INSTRUCTIONS_MD}"

# 4. Symlink alias browsermcp -> browser-mcp
ln -sfn "${BROWSER_MCP_DIR}" "${MCP_DIR}/browsermcp"
echo "Created symlink alias: ${MCP_DIR}/browsermcp -> browser-mcp"

# 5. Global rules: browser_mcp_troubleshooting.md
TROUBLESHOOTING_MD="${RULES_DIR}/browser_mcp_troubleshooting.md"
cat > "${TROUBLESHOOTING_MD}" <<'EOF'
# Browser MCP Guidelines & Troubleshooting

## How to Use Browser MCP Best
Browser MCP v2.3.0 uses a single unified browser batch command interface rather than multiple fine-grained MCP tools.

### Best Practices
- **Batch Execution (Crucial for Speed):**
  Combine predictable actions into one multi-line commands string:
  ```
  goto https://example.com/login
  wait getByLabel('Email')
  fill getByLabel('Email') "user@example.com"
  fill getByLabel('Password') "secret" --submit
  ```

- **Use `snapshot -i` for Compact Inspection:**
  - `snapshot` dumps full accessibility tree.
  - `snapshot -i` inspects interactive elements only.
  - `find "<text>"` searches without dumping tree.

- **Selector Priorities:**
  1. Semantic queries (`getByRole`, `getByLabel`, `getByPlaceholder`, `getByText`).
  2. Snapshot references (`e1`, `e12`) — invalidate on navigation/reload!
  3. CSS / XPath fallback.

- **Iframe / Dialog Workarounds:**
  - Keyboard emulation: `press Tab`, `press ArrowDown`, `press Enter`.
  - Capture visual state with `screenshot`.

## Troubleshooting Browser MCP
- **"No connection to browser extension":** the relay is loopback-only: the daemon binds the first free port of 9010-9012 (see `~/.browsermcp/daemon.json` next to the agent) and the extension dials that port on localhost. A remote Chrome needs the SSH tunnel + `browser-relay` from `docs/browser-mcp-pairing.md` — without it, "no connection" is expected.
- **First use asks to click Allow:** match the six-digit code in the extension popup and click Allow, then ask the agent to try again.
- **Cannot attach to tab:** Restricted internal page (`chrome://*`, blank New Tab). Navigate to HTTP/HTTPS first.
- **Snapshot refs fail (e12 not found):** Re-run `snapshot -i` to refresh element references.
- **One browser at a time:** a second Chrome replaces the first; the replaced one reconnects when the other goes away.
EOF
echo "Created rules: ${TROUBLESHOOTING_MD}"

# 6. Configure mcp_config.json in both config/ and antigravity-cli/
python3 - <<EOF
import json
import os

launcher = "${LAUNCHER}"
targets = [
    "${CONFIG_DIR}/mcp_config.json",
    "${AGY_CLI_DIR}/mcp_config.json",
]

for cfg_path in targets:
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    cfg = {}
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}

    servers = cfg.setdefault("mcpServers", {})
    servers["browser-mcp"] = {
        "command": launcher
    }
    servers["browsermcp"] = {
        "command": launcher
    }

    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    print(f"Updated mcp_config: {cfg_path}")
EOF

# 7. Check Boomtick live symlink if boomtick repository exists
BOOMTICK_CANDIDATES=(
  "${TARGET_HOME}/boomtick"
  "/srv/repos/boomtick"
  "${TARGET_HOME}/repos/boomtick"
)
for bt in "${BOOMTICK_CANDIDATES[@]}"; do
  if [ -d "${bt}/mcp/.mcp/schemas" ]; then
    ln -sfn "${bt}/mcp/.mcp/schemas" "${MCP_DIR}/boomtick-mcp"
    echo "Linked live schemas: ${MCP_DIR}/boomtick-mcp -> ${bt}/mcp/.mcp/schemas"
    if [ -f "${bt}/mcp/dist/index.js" ]; then
      ln -sfn "${bt}/mcp/dist/index.js" "${BIN_DIR}/boomtick-mcp"
      echo "Linked binary: ${BIN_DIR}/boomtick-mcp -> ${bt}/mcp/dist/index.js"
    fi
    break
  fi
done

echo "==> Browser MCP setup completed successfully."
