#!/bin/bash
# Sweep stale git worktrees and report stale branches.
# Usage: bash scripts/prune-worktrees.sh [--prune]
#
# A linked worktree is pruned only when ALL hold:
#   - it is not the tree the script runs from,
#   - it is not locked and not detached,
#   - its tree is clean (no uncommitted changes and no untracked
#     files at all — unknown files block pruning),
#   - its branch is landed: every branch commit is in origin/main,
#     or GitHub reports a MERGED PR for the branch head
#     (squash-merges rewrite SHAs, so ancestry alone misses them).
# Unmerged or non-empty-but-unlanded work is NEVER removed.
# After a worktree goes, its branch is deleted with `git branch -d`
# (which itself refuses unmerged branches: second safety).
# Standalone local branches (no worktree) are report-only always:
# the owner, not this script, decides they are done.
set -u

PRUNE=0
if [ "${1:-}" = "--prune" ]; then
    PRUNE=1
fi

TOPLEVEL="$(git rev-parse --show-toplevel)"
git fetch -q origin main 2>/dev/null \
    || echo "(fetch failed; judging against cached origin/main)"
HAVE_GH=0
command -v gh >/dev/null 2>&1 && HAVE_GH=1

# landed <branch> <head>: 0 when the branch's work is in origin/main
# (true merge) or its PR merged (squash/rebase merge).
landed() {
    git merge-base --is-ancestor "$2" origin/main 2>/dev/null && return 0
    if [ "$HAVE_GH" -eq 1 ]; then
        [ "$(gh pr list --head "$1" --state merged --json number \
            -q 'length' 2>/dev/null)" -gt 0 ] 2>/dev/null && return 0
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

path=""
head=""
branch=""
locked=0
flush_tree() {
    [ -z "$path" ] && return 0
    if [ "$path" = "$TOPLEVEL" ]; then
        echo "SELF  $path [$branch] -- the tree running this script, never touched"
    elif [ "$locked" -eq 1 ]; then
        echo "KEEP  $path [$branch] -- locked"
    elif [ -z "$branch" ]; then
        echo "KEEP  $path [(detached)] -- detached HEAD, owner decides"
    elif [ -n "$(git -C "$path" status --porcelain)" ]; then
        echo "KEEP  $path [$branch] -- dirty tree"
    elif [ "$(git rev-list --count "$head" --not origin/main \
            2>/dev/null || echo 1)" -eq 0 ]; then
        echo "KEEP  $path [$branch] -- empty branch, owner decides"
    elif landed "$branch" "$head"; then
        echo "PRUNE $path [$branch]"
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
