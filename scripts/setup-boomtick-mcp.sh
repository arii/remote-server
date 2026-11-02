#!/usr/bin/env bash
# Setup and configure Boomtick MCP (boomtick-mcp) for Antigravity.
# Idempotent — safe to re-run.
set -euo pipefail

TARGET_HOME="${TARGET_HOME:-${HOME}}"
GEMINI_DIR="${TARGET_HOME}/.gemini"
CONFIG_DIR="${GEMINI_DIR}/config"
AGY_CLI_DIR="${GEMINI_DIR}/antigravity-cli"
BIN_DIR="${AGY_CLI_DIR}/bin"
MCP_DIR="${AGY_CLI_DIR}/mcp"

GITHUB_OWNER="${GITHUB_OWNER:-arii}"
GITHUB_REPO="${GITHUB_REPO:-boomtick}"

echo "==> Setting up Boomtick MCP under ${GEMINI_DIR}..."

mkdir -p "${BIN_DIR}"
mkdir -p "${MCP_DIR}"

# 1. Live schema symlink (never a static copy): point at the boomtick
# checkout's generated schemas so tool contracts update with the repo.
BOOMTICK_CANDIDATES=(
  "${TARGET_HOME}/boomtick"
  "/srv/repos/boomtick"
  "${TARGET_HOME}/repos/boomtick"
)
BOOMTICK_DIR=""
for bt in "${BOOMTICK_CANDIDATES[@]}"; do
  if [ -d "${bt}/mcp/.mcp/schemas" ]; then
    BOOMTICK_DIR="${bt}"
    break
  fi
done

if [ -n "${BOOMTICK_DIR}" ]; then
  ln -sfn "${BOOMTICK_DIR}/mcp/.mcp/schemas" "${MCP_DIR}/boomtick-mcp"
  echo "Linked live schemas: ${MCP_DIR}/boomtick-mcp -> ${BOOMTICK_DIR}/mcp/.mcp/schemas"
else
  echo "Note: no boomtick checkout found; skipping live schema symlink."
fi

# 2. Server binary: prefer the global install (the S5 terminal image bakes
# @arii/boomtick-mcp), else the checkout's built entrypoint.
SERVER_COMMAND=""
if command -v boomtick-mcp >/dev/null 2>&1; then
  SERVER_COMMAND="$(command -v boomtick-mcp)"
  echo "Using global binary: ${SERVER_COMMAND}"
elif [ -n "${BOOMTICK_DIR}" ] && [ -f "${BOOMTICK_DIR}/mcp/dist/index.js" ]; then
  ln -sfn "${BOOMTICK_DIR}/mcp/dist/index.js" "${BIN_DIR}/boomtick-mcp"
  SERVER_COMMAND="${BIN_DIR}/boomtick-mcp"
  echo "Linked binary: ${SERVER_COMMAND} -> ${BOOMTICK_DIR}/mcp/dist/index.js"
else
  SERVER_COMMAND="boomtick-mcp"
  echo "Warning: no boomtick-mcp binary found; entry will use 'boomtick-mcp' from PATH." >&2
fi

# 3. Server entries in both the host-level and CLI-level configs.
# Secrets are never written here: GITHUB_TOKEN is taken from the
# environment when present, otherwise left out (see reminder below).
BOOMTICK_REPO_PATH="${BOOMTICK_REPO_PATH:-${BOOMTICK_DIR:-${TARGET_HOME}/boomtick}}"
GITHUB_TOKEN_VALUE="${GITHUB_TOKEN:-${BOOMTICK_GITHUB_TOKEN:-}}"
export TARGET_HOME CONFIG_DIR AGY_CLI_DIR SERVER_COMMAND
export BOOMTICK_REPO_PATH GITHUB_OWNER GITHUB_REPO GITHUB_TOKEN_VALUE
if [ -n "${BOOMTICK_DIR}" ]; then
  BOOMTICK_VENV_BIN="${BOOMTICK_DIR}/.venv/bin"
else
  BOOMTICK_VENV_BIN=""
fi
export BOOMTICK_VENV_BIN

python3 - <<'EOF'
import json
import os

command = os.environ["SERVER_COMMAND"]
home = os.environ["TARGET_HOME"]
path_parts = [
    p for p in [
        os.environ.get("BOOMTICK_VENV_BIN", ""),
        os.path.join(home, ".local/bin"),
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ] if p
]
env = {
    "BOOMTICK_REPO_PATH": os.environ["BOOMTICK_REPO_PATH"],
    "GITHUB_OWNER": os.environ["GITHUB_OWNER"],
    "GITHUB_REPO": os.environ["GITHUB_REPO"],
    "PATH": ":".join(path_parts),
}
token = os.environ.get("GITHUB_TOKEN_VALUE", "")
if token:
    env["GITHUB_TOKEN"] = token

targets = [
    os.path.join(os.environ["CONFIG_DIR"], "mcp_config.json"),
    os.path.join(os.environ["AGY_CLI_DIR"], "mcp_config.json"),
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
    servers["boomtick-mcp"] = {"command": command, "args": [], "env": env}
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    print(f"Updated mcp_config: {cfg_path}")
EOF

if [ -z "${GITHUB_TOKEN_VALUE}" ]; then
  echo "Reminder: no GITHUB_TOKEN in the environment, so the server entry has no token (public read-only GitHub ops only). Re-run with GITHUB_TOKEN set to add it."
fi

echo "==> Boomtick MCP setup completed successfully."
