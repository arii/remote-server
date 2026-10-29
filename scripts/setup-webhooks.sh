#!/usr/bin/env bash
# Create or update the GitHub repo webhooks that feed the buttons
# auto-pull endpoint — in repo code, no manual GitHub UI steps.
#
# Reads the payload URL and secret from the same .env the stack
# runs on (TERMINAL_DOMAIN, TERMINAL_PORT, WEBHOOK_SECRET), so the
# webhook GitHub calls and the secret the handler verifies can
# never drift apart. Defaults to both auto-pull repos; pass repo
# slugs (owner/repo) as arguments to override.
#
# Idempotent: for each repo it lists existing hooks, matches one by
# config.url, PATCHes it if present (re-sending the secret, since
# GitHub never returns it), and POSTs only when no match exists.
# Re-running never creates a duplicate. Events are push only — the
# handler ignores everything else, so GitHub should not send it.
#
# Auth: `gh` must be authenticated with a token that can manage
# repo webhooks (Administration: read and write, a.k.a.
# admin:repo_hook). Where to run it: the terminal container is the
# verified spot (`gh auth status` works there); from the host
# checkout, `npm run webhooks:setup` routes the script through it.
# If gh answers 403/404, that is the permission to fix — the script
# prints the hint instead of failing cryptically.
#
# Usage:
#   scripts/setup-webhooks.sh [--check] [owner/repo ...]
#   --check: list each repo's hooks and whether ours matches; no changes.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHECK=0
if [ "${1:-}" = "--check" ]; then CHECK=1; shift; fi
REPOS=("$@")
[ "${#REPOS[@]}" -eq 0 ] && REPOS=("arii/remote-server" "arii/tech-dancer" "arii/boomtick")

command -v gh >/dev/null 2>&1 || {
  echo "ERROR: gh not found. Run this where gh is authenticated (terminal container: npm run webhooks:setup)." >&2
  exit 1
}
command -v python3 >/dev/null 2>&1 || {
  echo "ERROR: python3 not found (used to parse gh api output)." >&2
  exit 1
}

# Same sourcing pattern as the buttons TD prefix: .env is optional
# (a missing file just leaves the required vars unset, caught below).
set -a
[ -f "$ROOT/.env" ] && . "$ROOT/.env"
set +a

GH_ERR="$(mktemp /tmp/gh_setup_err.XXXXXX)"
trap 'rm -f "$GH_ERR"' EXIT

fail=0
require() { # name, value
  if [ -z "${2:-}" ]; then echo "ERROR: $1 is unset in $ROOT/.env" >&2; fail=1; fi
}
require TERMINAL_DOMAIN "${TERMINAL_DOMAIN:-}"
require TERMINAL_PORT "${TERMINAL_PORT:-}"
require WEBHOOK_SECRET "${WEBHOOK_SECRET:-}"
case "${TERMINAL_DOMAIN:-}" in
  *yourname*) echo "ERROR: TERMINAL_DOMAIN still holds the .env.example placeholder" >&2; fail=1;;
esac
[ "$fail" -ne 0 ] && exit 1

URL="https://${TERMINAL_DOMAIN}:${TERMINAL_PORT}/buttons/api/webhook"

# Print the id of the hook in the JSON on stdin whose config.url
# matches $WEBHOOK_URL, or nothing. (Plain prints, no f-strings:
# this must also run on pre-3.12 Python, where f-string
# expressions cannot contain backslashes.)
matching_hook_id() {
  WEBHOOK_URL="$URL" python3 -c '
import sys, json, os
try:
    hooks = json.load(sys.stdin)
except Exception:
    hooks = []
for h in hooks:
    if isinstance(h, dict) and (h.get("config") or {}).get("url") == os.environ["WEBHOOK_URL"]:
        print(h.get("id", ""))
        break
'
}

for repo in "${REPOS[@]}"; do
  echo "== $repo =="
  if ! hooks_json="$(gh api "repos/$repo/hooks" 2>"$GH_ERR")"; then
    echo "ERROR: cannot list hooks for $repo (gh auth? Administration permission?). gh said:" >&2
    cat "$GH_ERR" >&2
    echo "HINT: the token gh uses needs Administration: read and write (admin:repo_hook) on $repo." >&2
    fail=1
    continue
  fi
  if [ "$CHECK" = 1 ]; then
    printf '%s' "$hooks_json" | WEBHOOK_URL="$URL" python3 -c '
import sys, json, os
try:
    hooks = json.load(sys.stdin)
except Exception:
    hooks = []
for h in hooks:
    if isinstance(h, dict):
        mark = " <== ours" if (h.get("config") or {}).get("url") == os.environ["WEBHOOK_URL"] else ""
        print("  hook " + str(h.get("id")) + ": active=" + str(h.get("active")) + " events=" + str(h.get("events")) + " url=" + str((h.get("config") or {}).get("url")) + mark)
print("  (secret is never returned by GitHub; re-apply to sync it)")
'
    continue
  fi
  hook_id="$(printf '%s' "$hooks_json" | matching_hook_id)"
  if [ -n "$hook_id" ]; then
    if gh api --method PATCH "repos/$repo/hooks/$hook_id" \
        -F active=true -f "events[]=push" \
        -f "config[url]=$URL" -f "config[content_type]=json" \
        -f "config[secret]=$WEBHOOK_SECRET" -f "config[insecure_ssl]=0" \
        >/dev/null 2>"$GH_ERR"; then
      echo "updated hook $hook_id (push only, url + secret synced)"
    else
      echo "ERROR: PATCH failed for $repo hook $hook_id:" >&2
      cat "$GH_ERR" >&2
      fail=1
    fi
  else
    if new_id="$(gh api --method POST "repos/$repo/hooks" \
        -f name=web -F active=true -f "events[]=push" \
        -f "config[url]=$URL" -f "config[content_type]=json" \
        -f "config[secret]=$WEBHOOK_SECRET" -f "config[insecure_ssl]=0" \
        2>"$GH_ERR" | python3 -c 'import sys, json; print(json.load(sys.stdin).get("id", ""))')"; then
      echo "created hook $new_id (creation triggers a GitHub ping — expect a pong in the buttons logs)"
    else
      echo "ERROR: POST failed for $repo:" >&2
      cat "$GH_ERR" >&2
      fail=1
    fi
  fi
done

if [ "$fail" -ne 0 ]; then
  echo "Done with errors (see above)." >&2
  exit 1
fi
echo "Done. Verify on the page: a ping or the next push shows up as an auto-pull line under the header."
