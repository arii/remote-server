"""PR triage agent: ready, resolve, and merge approved PRs.

Usage: pr_triage_agent.py owner/repo [pr...]
Deep Agent (Nebius) orchestrating deterministic gh/git tools that port
the manual merge runbook: per PR — state check, undraft, wait for CI,
merge; conflicting PRs go through worktree + resolver agent + push.
Optional PR numbers scope the run; otherwise all open PRs are triaged.
Every run is recorded to the ledger with tool results.
"""

import json
import os
import re
import subprocess
import sys
import time

from dotenv import load_dotenv
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from deepagents import create_deep_agent

import ledger

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9_.\-/]+$")
WORKTREE_RE = re.compile(r"^/tmp/pr\d+fix$")
HERE = os.path.dirname(os.path.abspath(__file__))
CHECKOUT = os.path.dirname(HERE)
MODEL_ID = "zai-org/GLM-5.3-Flash"
FAILING_CONCLUSIONS = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"}


def _run(cmd, timeout=180):
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout
    )


def _valid_repo(repo):
    return isinstance(repo, str) and bool(REPO_RE.match(repo))


def _valid_number(number):
    return (
        isinstance(number, int)
        and not isinstance(number, bool)
        and number > 0
    )


def _pr_json(repo, number):
    proc = _run(
        ["gh", "pr", "view", str(number), "--repo", repo, "--json",
         "number,title,headRefName,isDraft,reviewDecision,mergeable,"
         "mergeStateStatus,statusCheckRollup"]
    )
    if proc.returncode != 0:
        return None, (proc.stderr or "gh failed").strip()[:300]
    try:
        return json.loads(proc.stdout), ""
    except Exception:
        return None, "gh returned invalid JSON"


def _check_failures(pr):
    bad = []
    for c in pr.get("statusCheckRollup") or []:
        if not isinstance(c, dict):
            continue
        if (c.get("conclusion") or "").upper() in FAILING_CONCLUSIONS:
            bad.append(str(c.get("name") or "?"))
    return bad


@tool
def list_prs(repo: str) -> str:
    """List open PRs with draft/review/merge state.

    Args:
        repo: Repository in owner/name format.
    """
    if not _valid_repo(repo):
        return "REJECTED: repo must look like owner/name"
    proc = _run(
        ["gh", "pr", "list", "--repo", repo, "--state", "open", "--json",
         "number,title,isDraft,reviewDecision,mergeable,mergeStateStatus,"
         "headRefName"]
    )
    return (proc.stdout + proc.stderr).strip()[:6000] or "(no open PRs)"


@tool
def pr_merge_state(repo: str, number: int) -> str:
    """Merge-readiness state for one PR: draft, review decision,
    failing checks, mergeable flag. Read-only.

    Args:
        repo: Repository in owner/name format.
        number: PR number.
    """
    if not _valid_repo(repo) or not _valid_number(number):
        return "REJECTED: bad repo or number"
    pr, err = _pr_json(repo, number)
    if pr is None:
        return f"ERROR: {err}"
    failing = _check_failures(pr)
    return (
        f"PR #{pr.get('number')}: {pr.get('title')}\n"
        f"draft={pr.get('isDraft')} review={pr.get('reviewDecision')} "
        f"mergeable={pr.get('mergeable')} state={pr.get('mergeStateStatus')}\n"
        f"failing_checks={failing or 'none'}"
    )


@tool
def mark_ready(repo: str, number: int) -> str:
    """Mark a draft PR ready for review, then report its new state.
    Refuses PRs that are not drafts.

    Args:
        repo: Repository in owner/name format.
        number: PR number.
    """
    if not _valid_repo(repo) or not _valid_number(number):
        return "REJECTED: bad repo or number"
    pr, err = _pr_json(repo, number)
    if pr is None:
        return f"REFUSED: {err}"
    if not pr.get("isDraft"):
        return f"REFUSED: PR #{number} is not a draft"
    proc = _run(["gh", "pr", "ready", str(number), "--repo", repo])
    if proc.returncode != 0:
        return f"FAILED: {proc.stderr.strip()[:300]}"
    time.sleep(5)
    pr2, err2 = _pr_json(repo, number)
    if pr2 is None:
        return f"READY (unverified): {err2}"
    return (
        f"READY: PR #{number} draft={pr2.get('isDraft')} "
        f"mergeable={pr2.get('mergeable')} state={pr2.get('mergeStateStatus')}"
    )


@tool
def wait_checks(repo: str, number: int) -> str:
    """Wait for a PR's CI checks to complete. Returns PASS or FAIL
    with the failing check names. Slow (minutes); call at most once
    per PR per state change.

    Args:
        repo: Repository in owner/name format.
        number: PR number.
    """
    if not _valid_repo(repo) or not _valid_number(number):
        return "REJECTED: bad repo or number"
    proc = _run(
        ["gh", "pr", "checks", str(number), "--repo", repo,
         "--watch", "--fail-fast"],
        timeout=660,
    )
    out = (proc.stdout + proc.stderr).strip()[-800:]
    if proc.returncode != 0:
        return f"FAIL: PR #{number} checks failed:\n{out}"
    return f"PASS: PR #{number} checks green."


@tool
def merge_pr(repo: str, number: int) -> str:
    """Merge one PR after re-validating it is approved, green,
    mergeable, and not a draft. Refuses anything else.

    Args:
        repo: Repository in owner/name format.
        number: PR number.
    """
    if not _valid_repo(repo) or not _valid_number(number):
        return "REJECTED: bad repo or number"
    pr, err = _pr_json(repo, number)
    if pr is None:
        return f"REFUSED: {err}"
    if pr.get("isDraft"):
        return f"REFUSED: PR #{number} is a draft"
    if pr.get("reviewDecision") != "APPROVED":
        return f"REFUSED: PR #{number} review={pr.get('reviewDecision')}"
    failing = _check_failures(pr)
    if failing:
        return f"REFUSED: PR #{number} failing checks: {failing}"
    if pr.get("mergeable") != "MERGEABLE":
        return f"REFUSED: PR #{number} mergeable={pr.get('mergeable')}"
    proc = _run(
        ["gh", "pr", "merge", str(number), "--repo", repo, "--merge"],
        timeout=180,
    )
    out = (proc.stdout + proc.stderr).strip()[:500]
    if proc.returncode != 0:
        return f"FAILED: PR #{number} merge error: {out}"
    return f"MERGED: PR #{number} ({pr.get('title')}). {out}"


@tool
def worktree_prepare(repo: str, number: int) -> str:
    """Create a fresh worktree of a PR's branch merged with main.
    Idempotent: removes any previous worktree for this PR first.
    Reports CONFLICT with file list, or CLEAN.

    Args:
        repo: Repository in owner/name format.
        number: PR number.
    """
    if not _valid_repo(repo) or not _valid_number(number):
        return "REJECTED: bad repo or number"
    pr, err = _pr_json(repo, number)
    if pr is None:
        return f"REFUSED: {err}"
    head = pr.get("headRefName") or ""
    if not BRANCH_RE.match(head):
        return f"REFUSED: bad branch name {head!r}"
    wt = f"/tmp/pr{number}fix"
    _run(["git", "-C", CHECKOUT, "fetch", "origin", "main"])
    _run(["git", "-C", CHECKOUT, "worktree", "remove", "--force", wt])
    _run(["git", "-C", CHECKOUT, "branch", "-D", head])
    proc = _run(
        ["git", "-C", CHECKOUT, "worktree", "add", wt, "-B", head,
         f"origin/{head}"]
    )
    if proc.returncode != 0:
        return f"FAILED: worktree add: {proc.stderr.strip()[:300]}"
    proc = _run(["git", "-C", wt, "merge", "--no-commit", "--no-ff",
                 "origin/main"])
    if proc.returncode != 0:
        u = _run(["git", "-C", wt, "diff", "--name-only",
                  "--diff-filter=U"])
        files = u.stdout.strip().replace("\n", ", ")[:400]
        return f"CONFLICT in {wt}: {files or 'unknown files'}"
    return f"CLEAN: {wt} merged main with no conflicts (uncommitted)."


@tool
def resolve_conflicts(worktree: str) -> str:
    """Run the resolver agent on a conflicted worktree (path must
    look like /tmp/prNfix). Returns the agent's report; verify with
    push_branch, which refuses on leftover markers.

    Args:
        worktree: Worktree path from worktree_prepare.
    """
    if not WORKTREE_RE.match(worktree or ""):
        return "REJECTED: worktree must look like /tmp/prNfix"
    exe = os.path.join(HERE, ".venv", "bin", "python")
    proc = _run(
        [exe, os.path.join(HERE, "resolve_conflicts.py"), worktree],
        timeout=900,
    )
    out = (proc.stdout + proc.stderr).strip()[-1500:]
    return f"exit={proc.returncode}\n{out}"


@tool
def push_branch(worktree: str, branch: str) -> str:
    """Commit the merge resolution and push to the PR branch.
    Refuses when conflict markers remain. Verifies the push.

    Args:
        worktree: Worktree path from worktree_prepare.
        branch: PR branch name to push.
    """
    if not WORKTREE_RE.match(worktree or ""):
        return "REJECTED: worktree must look like /tmp/prNfix"
    if not BRANCH_RE.match(branch or ""):
        return "REJECTED: bad branch name"
    chk = _run(["git", "-C", worktree, "diff", "--check"])
    if chk.returncode != 0 or (chk.stdout + chk.stderr).strip():
        return f"REFUSED: markers remain:\n{(chk.stdout + chk.stderr)[:400]}"
    _run(["git", "-C", worktree, "add", "-A"])
    cmt = _run(
        ["git", "-C", worktree, "-c", "user.name=s5-triage",
         "-c", "user.email=triage@s5.local", "commit", "--no-edit"]
    )
    if cmt.returncode != 0:
        return f"FAILED: commit: {cmt.stderr.strip()[:300]}"
    psh = _run(["git", "-C", worktree, "push", "origin", branch],
               timeout=180)
    if psh.returncode != 0:
        return f"FAILED: push: {psh.stderr.strip()[:300]}"
    return f"PUSHED: resolution to {branch}."


@tool
def worktree_cleanup(worktree: str) -> str:
    """Remove a worktree created by worktree_prepare.

    Args:
        worktree: Worktree path from worktree_prepare.
    """
    if not WORKTREE_RE.match(worktree or ""):
        return "REJECTED: worktree must look like /tmp/prNfix"
    _run(["git", "-C", CHECKOUT, "worktree", "remove", "--force",
          worktree])
    return f"CLEANED: {worktree} removed."


SYSTEM = """You triage and merge approved PRs, one PR at a time, in order.
For EACH target PR:
1. pr_merge_state. If review is not APPROVED, or checks are failing,
   record SKIP + reason and move on. Pending checks are not failing.
2. If draft: mark_ready, then wait_checks once. If checks FAIL,
   record SKIP + reason and move on.
3. Re-check pr_merge_state. If mergeable is CONFLICTING:
   worktree_prepare, then resolve_conflicts on its worktree, then
   push_branch with the PR branch name, then wait_checks, then
   pr_merge_state again. If still conflicting, record SKIP + reason.
4. merge_pr. Record its EXACT result line.
5. worktree_cleanup for any worktree you made.
Rules: finish one PR fully before starting the next. Never write
MERGED, READY, or PUSHED unless that tool returned that exact word
for that PR; the final report is verified against tool results.
A tool refusal is final for that PR this run — record and move on.
FINAL: one line per PR: MERGED, or SKIP/FAILED + reason, quoting
only what tools returned."""


def main() -> int:
    if len(sys.argv) < 2 or not REPO_RE.match(sys.argv[1]):
        print("usage: pr_triage_agent.py owner/repo [pr...]",
              file=sys.stderr)
        return 2
    repo = sys.argv[1]
    wanted = []
    for a in sys.argv[2:]:
        if not a.isdigit() or int(a) <= 0:
            print(f"bad PR number: {a}", file=sys.stderr)
            return 2
        wanted.append(int(a))
    started = time.time()
    load_dotenv("/srv/remote-server/.env")
    if not os.environ.get("NEBIUS_API_KEY"):
        print("NEBIUS_API_KEY is not set", file=sys.stderr)
        return 1
    model = ChatOpenAI(
        model=MODEL_ID,
        base_url="https://api.studio.nebius.com/v1",
        api_key=os.environ["NEBIUS_API_KEY"],
        temperature=0,
        # Reasoning model: headroom for hidden reasoning tokens.
        max_tokens=4000,
    )
    agent = create_deep_agent(
        model=model,
        tools=[list_prs, pr_merge_state, mark_ready, wait_checks,
               merge_pr, worktree_prepare, resolve_conflicts,
               push_branch, worktree_cleanup],
        system_prompt=SYSTEM,
        checkpointer=MemorySaver(),
    )
    scope = f" Only triage these PRs: {wanted}." if wanted else ""
    thread = "triage-" + repo.replace("/", "-")
    result = agent.invoke(
        {"messages": [{"role": "user", "content":
                       f"Triage open PRs on {repo} and merge every "
                       f"approved, green, mergeable one.{scope}"}]},
        config={"configurable": {"thread_id": thread},
                "recursion_limit": 60},
    )
    calls = sum(
        len(getattr(m, "tool_calls", []) or [])
        for m in result["messages"]
    )
    outcomes = []
    for m in result["messages"]:
        if m.__class__.__name__ == "ToolMessage" and str(m.content).strip():
            if getattr(m, "name", "") in ("mark_ready", "merge_pr",
                                          "push_branch"):
                outcomes.append(
                    f"{m.name}: "
                    f"{str(m.content).strip().splitlines()[0][:200]}"
                )
    ledger.record(
        "pr_triage_agent.py", f"nebius:{MODEL_ID}", thread,
        result["messages"], started, extra={"prs": wanted or "all"},
    )
    print(f"tool_calls_made: {calls}")
    print("REPORT (authoritative, from tool results):")
    if outcomes:
        for line in outcomes:
            print(f"  {line}")
    else:
        print("  (no ready/merge/push action returned)")
    print("FINAL:", result["messages"][-1].content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
