#!/usr/bin/env bash
# Persist gh CLI login to ~/.config/gh/hosts.yml from container env.
# Needed by processes that don't inherit container env (harness MCP
# servers); normal shells already auth via GH_TOKEN/GITHUB_TOKEN.
# Idempotent — safe to re-run (e.g. after recreating the gh-data volume).
set -euo pipefail

python3 - <<'EOF'
import os

d = os.path.expanduser("~/.config/gh")
os.makedirs(d, exist_ok=True)
p = os.path.join(d, "hosts.yml")
tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
if not tok:
    raise SystemExit("GH_TOKEN/GITHUB_TOKEN is not set; add BOOMTICK_GITHUB_TOKEN to .env and recreate the terminal container.")
with open(p, "w") as f:
    f.write("github.com:\n    user: arii\n    oauth_token: %s\n    git_protocol: https\n" % tok)
os.chmod(p, 0o600)
print("wrote %s (mode 600)" % p)
EOF
