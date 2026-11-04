"""Deep Agent merge-conflict resolver (repair worktree).

Usage: resolve_conflicts.py <worktree-path> [base-ref]
The agent merges the base ref (default origin/main), resolves every
conflict on real disk, and verifies zero markers remain. Commit +
replacement PR happen outside via BoomTick MCP tools after human
diff review. Works for any repo, any PR, any base branch.
"""

import os
import subprocess
import sys
import threading
import time

from dotenv import load_dotenv
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend

import cli
import ledger
import model_router
import observe

MODEL_ID = os.environ.get("NEBIUS_MODEL", "nvidia/Nemotron-3_5-Lightning")
DEFAULT_BASE = "origin/main"
DEFAULT_TIMEOUT = 1800
RECURSION_LIMIT = 100

WORKTREE = None
BASE = DEFAULT_BASE


def _timeout():
    """Wall-clock bound in seconds (RESOLVER_TIMEOUT override)."""
    try:
        return max(60, int(os.environ.get(
            "RESOLVER_TIMEOUT", DEFAULT_TIMEOUT)))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT


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
def git_merge_base() -> str:
    """Merge the base ref into the repair branch without committing."""
    return _git("merge", "--no-commit", "--no-ff", BASE)


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
    global WORKTREE, BASE
    if len(sys.argv) < 2:
        return cli.usage_error(
            "usage: resolve_conflicts.py <worktree-path> [base-ref]")
    WORKTREE = sys.argv[1]
    BASE = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_BASE
    started = time.time()
    load_dotenv(os.path.join(cli.repo_root(), ".env"))
    if not os.environ.get("NEBIUS_API_KEY"):
        print("NEBIUS_API_KEY is not set", file=sys.stderr)
        return 1
    if not os.path.isdir(os.path.join(WORKTREE, ".git")) and not os.path.isfile(
        os.path.join(WORKTREE, ".git")
    ):
        print(f"not a worktree: {WORKTREE}", file=sys.stderr)
        return 1
    try:
        spec = model_router.resolve_llm(prefer="nebius")
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    # Cap checked above; keep this entrypoint's pinned model/endpoint.
    spec = dict(spec, model=MODEL_ID,
                base_url="https://api.studio.nebius.com/v1")
    kwargs = model_router.chat_kwargs(spec)
    model = ChatOpenAI(
        api_key=os.environ["NEBIUS_API_KEY"],
        # Reasoning model: needs headroom for hidden reasoning tokens
        # plus the visible reply + tool call.
        max_tokens=4000,
        **kwargs,
    )
    thread = "resolve-" + os.path.basename(os.path.normpath(WORKTREE))
    label = f"nebius:{MODEL_ID}"
    recorder = observe.TraceRecorder(
        observe.default_trace_path("resolve_conflicts", thread))
    recorder.run_start("resolve_conflicts.py", thread, label)
    agent = create_deep_agent(
        model=model,
        tools=[git_status, git_merge_base, git_show_stage, git_diff_check],
        system_prompt=(
            "You resolve a git merge conflict in the current directory "
            "(a repair worktree; you see it as '.'). Modify ONLY files "
            "that contain conflict markers from the merge. Never touch .git."
            "\nSteps: 1) git_status. 2) git_merge_base. 3) For each "
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
        middleware=observe.trace_middleware(recorder),
    )
    outcome = {}

    def _invoke():
        try:
            outcome["result"] = agent.invoke(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                f"Merge {BASE} into this repair branch and "
                                "resolve all merge conflicts."
                            ),
                        }
                    ]
                },
                config={"configurable": {"thread_id": thread},
                        "recursion_limit": RECURSION_LIMIT},
            )
        except Exception as exc:
            outcome["error"] = exc

    # Daemon thread, not a pool: pool shutdown would wait out the very
    # hang the timeout exists to terminate. The abandoned worker dies
    # with the process right after the timeout row is ledgered.
    timeout = _timeout()
    worker = threading.Thread(target=_invoke, daemon=True)
    worker.start()
    worker.join(timeout=timeout)
    if worker.is_alive():
        recorder.run_end(False)
        recorder.close()
        usage, calls = recorder.totals()
        ledger.record(
            "resolve_conflicts.py",
            label,
            thread,
            [],
            started,
            extra={"worktree": WORKTREE, "base": BASE,
                   "trace": recorder.path,
                   "error": f"timed out after {timeout}s"},
            usage=usage,
            calls=calls,
        )
        print(f"resolve_conflicts timed out after {timeout}s",
              file=sys.stderr)
        return 1
    if "error" in outcome:
        recorder.run_end(False)
        recorder.close()
        raise outcome["error"]
    result = outcome["result"]
    recorder.run_end(True)
    recorder.close()
    usage, calls = recorder.totals()
    ledger.record(
        "resolve_conflicts.py",
        label,
        thread,
        result["messages"],
        started,
        extra={"worktree": WORKTREE, "base": BASE,
               "trace": recorder.path},
        usage=usage,
        calls=calls,
    )
    print(result["messages"][-1].content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
