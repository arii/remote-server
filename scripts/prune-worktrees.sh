#!/bin/bash
# Sweep stale git worktrees and report stale branches.
# Usage: bash scripts/prune-worktrees.sh [--prune] [--agent-mode]
#
# --agent-mode (queue runner only): agent-owned branches
#   (agent/issue-*, queue-retry/*, kiro/issue-*) are pruned even with
#   untracked/ignored files present — runner artifacts are disposable.
#   Tracked uncommitted changes still block. Interactive closeout must
#   NOT pass this flag: unknown files in agent trees block pruning.
#
# A linked worktree is pruned only when ALL hold:
#   - it is not the tree the script runs from,
#   - it is not locked and not detached,
#   - its tree is clean (no uncommitted changes and no untracked
#     files at all — unknown files block pruning),
#   - its branch is landed: every branch commit is in origin/main,
#     or GitHub reports a MERGED PR for the branch head
#     (squash-merges rewrite SHAs, so ancestry alone misses them).
# Every unknown or ignored file blocks deletion, including agent artifacts.
# Main/master, locked, detached and operator trees are never removed.
# Standalone local branches remain report-only.
set -u

PRUNE=0
AGENT_MODE=0
for arg in "$@"; do
    case "$arg" in
        --prune) PRUNE=1 ;;
        --agent-mode) AGENT_MODE=1 ;;
    esac
done

TOPLEVEL="$(git rev-parse --show-toplevel)" || exit 1
git fetch -q origin main 2>/dev/null || { echo "KEEP all -- fetch failed; no deletion against stale refs"; exit 1; }
HAVE_GH=0
command -v gh >/dev/null 2>&1 && HAVE_GH=1

# landed <branch> <head>: 0 when the branch's work is in origin/main
# (true merge) or its PR merged (squash/rebase merge).
landed() {
    git merge-base --is-ancestor "$2" origin/main 2>/dev/null && return 0
    if [ "$HAVE_GH" -eq 1 ]; then
        # Match the actual merged head, never just a reused branch name.
        gh pr list --head "$1" --state merged --json headRefOid \
            --jq '.[].headRefOid' 2>/dev/null | grep -Fxq "$2" && return 0
    fi
    return 1
}

# delete_branch <branch> <tip>: -d first; if that refuses only
# because the current checkout is not on main, -D is still safe
# when every branch commit is provably inside origin/main.
# Anything else is kept and reported.
delete_branch() {
    git branch -d "$1" 2>/dev/null && return 0
    if git merge-base --is-ancestor "$2" origin/main 2>/dev/null; then
        git branch -D "$1" 2>/dev/null && return 0
    fi
    echo "      (branch $1 kept: not fully contained in origin/main)"
    return 1
}

agent_owned() {
    case "$1" in
        agent/issue-*|queue-retry/*|kiro/issue-*) return 0 ;;
    esac
    return 1
}

status_file="$(mktemp)" || exit 1
trap 'rm -f "$status_file"' EXIT
path=""
head=""
branch=""
locked=0
flush_tree() {
    [ -z "$path" ] && return 0
    if [ "$path" = "$TOPLEVEL" ]; then
        echo "SELF  $path [$branch] -- the tree running this script, never touched"
    elif [ "$branch" = "main" ] || [ "$branch" = "master" ]; then
        echo "KEEP  $path [$branch] -- protected branch"
    elif [ "$locked" -eq 1 ]; then
        echo "KEEP  $path [$branch] -- locked"
    elif [ "$(basename "$path")" = "queue-main" ]; then
        echo "KEEP  $path [$branch] -- operator runner worktree, never pruned"
    elif [ -z "$branch" ]; then
        echo "KEEP  $path [(detached)] -- detached HEAD, owner decides"
    elif ! git -C "$path" status --porcelain --untracked-files=all --ignored > "$status_file" 2>/dev/null; then
        echo "KEEP  $path [$branch] -- status failed"
    elif [ "$AGENT_MODE" -eq 1 ] && agent_owned "$branch"; then
        if [ -n "$(git -C "$path" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
            echo "KEEP  $path [$branch] -- dirty tracked files"
        elif landed "$branch" "$head"; then
            echo "PRUNE $path [$branch] (agent-mode: untracked artifacts ignored)"
            if [ "$PRUNE" -eq 1 ]; then
                if git worktree remove --force "$path" 2>/dev/null; then
                    delete_branch "$branch" "$head"
                else
                    echo "      (worktree remove failed; left in place)"
                fi
            fi
        else
            echo "KEEP  $path [$branch] -- branch not merged"
        fi
    elif [ -s "$status_file" ]; then
        echo "KEEP  $path [$branch] -- dirty or unknown files (including ignored)"
    elif ! agent_owned "$branch" && [ "$(git rev-list --count "$head" --not origin/main 2>/dev/null || echo 1)" -eq 0 ]; then
        echo "KEEP  $path [$branch] -- empty non-agent branch, owner decides"
    elif landed "$branch" "$head"; then
        echo "PRUNE $path [$branch]"
        if [ "$PRUNE" -eq 1 ]; then
            if git worktree remove "$path" 2>/dev/null; then
                delete_branch "$branch" "$head"
            else
                echo "      (worktree remove failed; left in place)"
            fi
        fi
    else
        echo "KEEP  $path [$branch] -- branch not merged"
    fi
}

while IFS= read -r line; do
    case "$line" in
        worktree\ *)
            flush_tree
            path="${line#worktree }"
            head=""; branch=""; locked=0
            ;;
        HEAD\ *) head="${line#HEAD }" ;;
        branch\ *) branch="${line#branch refs/heads/}" ;;
        locked*) locked=1 ;;
        bare*) branch="" ;;
    esac
done < <(git worktree list --porcelain)
flush_tree

echo "--- branches without a worktree, merged into origin/main ---"
git worktree list --porcelain | grep '^branch ' | sort -u > /tmp/prune_wt_branches.$$
git branch --format='%(refname:short)' | while IFS= read -r b; do
    [ "$b" = "main" ] && continue
    if grep -qx "branch refs/heads/$b" /tmp/prune_wt_branches.$$ 2>/dev/null; then
        continue
    fi
    tip="$(git rev-parse "$b" 2>/dev/null)" || continue
    if landed "$b" "$tip"; then
        echo "STALE $b -- landed, no worktree; owner deletes with: git branch -d $b"
    fi
done
rm -f /tmp/prune_wt_branches.$$
[ "$PRUNE" -eq 0 ] && echo "(report only; re-run with --prune to remove PRUNE lines)"

exit 0
