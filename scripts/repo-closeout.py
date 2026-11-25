#!/usr/bin/env python3
"""Safe post-work closeout. Default is report-only; --apply only syncs/prunes.

Never stages, commits, pushes, stashes, resets, switches a branch, or restarts a
service. Dirty work is reported for the owning task to review and commit.
"""
import argparse
import json
from pathlib import Path
import subprocess


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], text=True, capture_output=True, check=True
    ).stdout.strip()


def closeout(repo, apply=False):
    repo = Path(repo).resolve()
    result = {"repo": str(repo), "actions": [], "needs_owner": []}
    try:
        git(repo, "fetch", "origin", "main")
        branch = git(repo, "branch", "--show-current")
        changes = git(repo, "status", "--porcelain", "--untracked-files=all", "--ignored")
        result.update(branch=branch, changes=changes.splitlines())
        if changes:
            result["needs_owner"].append("Review uncommitted work; never auto-commit unknown files")
        elif branch == "main":
            ahead = int(git(repo, "rev-list", "--count", "origin/main..HEAD"))
            if ahead:
                result["needs_owner"].append("Main has local commits; no reset or push")
            elif apply:
                git(repo, "-c", "core.hooksPath=/dev/null", "merge", "--ff-only", "origin/main")
                result["actions"].append("clean main fast-forwarded with deployment hooks disabled")
            else:
                result["actions"].append("clean main eligible for fast-forward")
        else:
            result["needs_owner"].append("Active/detached checkout kept on its current branch")
        script = Path(__file__).with_name("prune-worktrees.sh")
        command = ["bash", str(script)] + (["--prune"] if apply else [])
        sweep = subprocess.run(command, cwd=repo, text=True, capture_output=True)
        result["worktrees"] = sweep.stdout.splitlines()
        if sweep.stderr:
            result["worktree_errors"] = sweep.stderr.splitlines()
        if sweep.returncode:
            result["needs_owner"].append("Worktree sweep failed; inspect output")
        result["head"] = git(repo, "rev-parse", "HEAD")
        result["remaining_changes"] = git(repo, "status", "--porcelain", "--untracked-files=all", "--ignored").splitlines()
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        result["needs_owner"].append(f"Closeout stopped without destructive recovery: {error}")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repos", nargs="+")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    results = [closeout(repo, args.apply) for repo in args.repos]
    print(json.dumps(results, indent=2))
    return 1 if any(r["needs_owner"] for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
