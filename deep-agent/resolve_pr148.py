"""Deep Agent merge-conflict resolver (repair worktree).

Usage: resolve_pr148.py [worktree-path]
Defaults to the #148 repair worktree. The agent performs the merge,
resolves every conflict on real disk, and verifies zero markers remain.
Commit + replacement PR happen outside via BoomTick MCP tools after
human diff review.
"""

import os
import subprocess
import sys

from dotenv import load_dotenv
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend

DEFAULT_WORKTREE = "/tmp/boomtick-worktrees/boomtick-mcp-rescue-148"
WORKTREE = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_WORKTREE


def _git(*args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", WORKTREE, *args],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return f"exit={proc.returncode}\n{(proc.stdout + proc.stderr).strip()[:4000]}"


@tool
def git_status() -> str:
    """Show short git status of the repair worktree."""
    return _git("status", "--short")


@tool
def git_merge_main() -> str:
    """Merge origin/main into the repair branch without committing."""
    return _git("merge", "--no-commit", "--no-ff", "origin/main")


@tool
def git_show_stage(stage: str, path: str) -> str:
    """Show a merge-stage blob: stage 1=base, 2=ours, 3=theirs. Read-only."""
    if stage not in ("1", "2", "3"):
        return "REJECTED: stage must be 1, 2, or 3"
    if not path or path.startswith("/") or ".." in path.split("/"):
        return "REJECTED: path must be relative without .."
    return _git("show", f":{stage}:{path}")


@tool
def git_diff_check() -> str:
    """Report leftover conflict markers and whitespace errors."""
    out = _git("diff", "--check")
    return out if out.strip("exit=0\n ") else "CLEAN: no markers or whitespace errors"


def main() -> int:
    load_dotenv("/srv/remote-server/.env")
    if not os.environ.get("NEBIUS_API_KEY"):
        print("NEBIUS_API_KEY is not set", file=sys.stderr)
        return 1
    if not os.path.isdir(os.path.join(WORKTREE, ".git")) and not os.path.isfile(
        os.path.join(WORKTREE, ".git")
    ):
        print(f"not a worktree: {WORKTREE}", file=sys.stderr)
        return 1
    model = ChatOpenAI(
        model="zai-org/GLM-5.3-Flash",
        base_url="https://api.studio.nebius.com/v1",
        api_key=os.environ["NEBIUS_API_KEY"],
        temperature=0,
        # Reasoning model: needs headroom for hidden reasoning tokens
        # plus the visible reply + tool call.
        max_tokens=4000,
    )
    agent = create_deep_agent(
        model=model,
        tools=[git_status, git_merge_main, git_show_stage, git_diff_check],
        system_prompt=(
            "You resolve a git merge conflict in the current directory "
            "(a repair worktree; you see it as '.'). Modify ONLY files "
            "that contain conflict markers from the merge. Never touch .git."
            "\nSteps: 1) git_status. 2) git_merge_main. 3) For each "
            "conflicted file: read it, resolve every marker block by "
            "combining both sides' intent (for add/add conflicts use "
            "git_show_stage to see ours=2 and theirs=3, then union them "
            "sensibly with no duplication), apply with edit_file using "
            "exact matches. 4) git_diff_check until CLEAN. 5) Final "
            "message: per-file summary of what you kept from each side."
            "\nRules: no unrelated edits; when a hunk is ambiguous, keep "
            "both sides' content in a sensible order instead of deleting; "
            "never leave markers behind."
        ),
        backend=FilesystemBackend(root_dir=WORKTREE, virtual_mode=True),
        checkpointer=MemorySaver(),
    )
    thread = "resolve-" + os.path.basename(os.path.normpath(WORKTREE))
    result = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Merge origin/main into this repair branch and "
                        "resolve all merge conflicts."
                    ),
                }
            ]
        },
        config={"configurable": {"thread_id": thread}},
    )
    print(result["messages"][-1].content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
