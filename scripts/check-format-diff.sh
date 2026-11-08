#!/usr/bin/env bash
# Diff-aware ruff format check (issue #1176).
#
# Checks `ruff format` only for Python files changed vs origin/main
# (plus untracked .py files) — never a mass reformat. The tree has
# ~170 files that fail `ruff format --check` today; reformatting them
# all at once would churn every lane, so the gate covers the diff and
# the backlog shrinks file-by-file as lanes touch code.
#
#   scripts/check-format-diff.sh [BASE]
#   FORMAT_BASE=<sha> scripts/check-format-diff.sh
#
# BASE defaults to the merge-base of HEAD and origin/main; when that
# ref is unavailable (no origin remote) it falls back to HEAD, i.e.
# uncommitted changes only. Needs ruff on PATH (CI installs
# ruff==0.17.4 to match .github/workflows/ci.yml; locally:
# pip install "ruff==0.17.4").
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BASE="${1:-${FORMAT_BASE:-}}"
if [[ -z "$BASE" ]]; then
    if BASE="$(git merge-base HEAD origin/main 2>/dev/null)"; then
        :
    else
        BASE="HEAD"
        echo "check-format-diff: no origin/main ref; checking uncommitted changes only" >&2
    fi
fi

files=()
while IFS= read -r -d '' f; do
    files+=("$f")
done < <(git diff --name-only --diff-filter=ACMRTU -z "$BASE" -- '*.py')
while IFS= read -r -d '' f; do
    files+=("$f")
done < <(git ls-files --others --exclude-standard -z -- '*.py')

if command -v ruff >/dev/null 2>&1; then
    RUFF=(ruff)
elif python3 -m ruff --version >/dev/null 2>&1; then
    RUFF=(python3 -m ruff)
else
    echo 'check-format-diff: ruff not found (CI: pip install "ruff==0.17.4")' >&2
    exit 2
fi

if [[ ${#files[@]} -eq 0 ]]; then
    echo "check-format-diff: no Python files changed vs $BASE — skipping"
    exit 0
fi
exec "${RUFF[@]}" format --check -- "${files[@]}"
