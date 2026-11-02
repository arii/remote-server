"""Merge approved PRs: triage open PRs and merge the ready ones.

Usage: merge_approved.py owner/repo
The agent lists open PRs, checks each one's review/CI/merge state,
and merges via merge_pr — which re-validates deterministically and
refuses anything not (approved + green + mergeable + not draft).
Local 8b model; every run is recorded to the ledger.
"""

import json
import re
import subprocess
import sys
import time

from langchain.agents import create_agent
from langchain_core.tools import tool
from langchain_ollama import ChatOllama

import ledger

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
MODEL_ID = "qwen3:8b"
FAILING_CONCLUSIONS = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"}


def _gh(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", *args], capture_output=True, text=True, timeout=timeout
    )


def _valid_repo(repo: str) -> bool:
    return bool(REPO_RE.match(repo))


def _valid_number(number: int) -> bool:
    return isinstance(number, int) and not isinstance(number, bool) and number > 0


def _pr_json(repo: str, number: int):
    proc = _gh(
        "pr", "view", str(number), "--repo", repo, "--json",
        "number,title,isDraft,reviewDecision,mergeable,mergeStateStatus,"
        "statusCheckRollup",
    )
    if proc.returncode != 0:
        return None, (proc.stderr or "gh failed").strip()[:300]
    try:
        return json.loads(proc.stdout), ""
    except Exception:
        return None, "gh returned invalid JSON"


def _check_failures(pr: dict) -> list:
    bad = []
    for c in pr.get("statusCheckRollup") or []:
        if not isinstance(c, dict):
            continue
        if (c.get("conclusion") or "").upper() in FAILING_CONCLUSIONS:
            bad.append(str(c.get("name") or "?"))
    return bad


@tool
def list_prs(repo: str) -> str:
    """List open PRs with draft/review/merge state for a repository.

    Args:
        repo: Repository in owner/name format.
    """
    if not _valid_repo(repo):
        return "REJECTED: repo must look like owner/name"
    proc = _gh(
        "pr", "list", "--repo", repo, "--state", "open", "--json",
        "number,title,isDraft,reviewDecision,mergeable,mergeStateStatus,"
        "headRefName",
    )
    return (proc.stdout + proc.stderr).strip()[:6000] or "(no open PRs)"


@tool
def pr_status(repo: str, number: int) -> str:
    """Get merge-readiness state for one PR: draft, review decision,
    failing checks, mergeable flag.

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
    proc = _gh(
        "pr", "merge", str(number), "--repo", repo, "--merge",
        timeout=180,
    )
    out = (proc.stdout + proc.stderr).strip()[:500]
    if proc.returncode != 0:
        return f"FAILED: PR #{number} merge error: {out}"
    return f"MERGED: PR #{number} ({pr.get('title')}). {out}"


def main() -> int:
    if len(sys.argv) < 2 or not REPO_RE.match(sys.argv[1]):
        print("usage: merge_approved.py owner/repo", file=sys.stderr)
        return 2
    repo = sys.argv[1]
    started = time.time()
    model = ChatOllama(
        model=MODEL_ID,
        base_url="http://ollama:11434",
        temperature=0,
    )
    agent = create_agent(
        model=model,
        tools=[list_prs, pr_status, merge_pr],
        system_prompt=(
            "You triage and merge approved PRs. Call list_prs once "
            "for the requested repo, then pr_status once per EVERY "
            "open PR number in the list output (skip none, check "
            "all), then merge_pr once per PR that "
            "looks ready — the tool re-validates and refuses the "
            "rest, which is a normal outcome, not an error. Never "
            "write MERGED for a PR unless the merge_pr tool "
            "actually returned MERGED for it; the report is "
            "verified against tool results. "
            "Always invoke tools through the function-calling "
            "interface. Never write a tool call out as JSON text. "
            "Final message: one line per PR with MERGED, REFUSED, "
            "or FAILED plus the reason."
        ),
    )
    thread_id = "merge-" + repo.replace("/", "-")
    result = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": f"Triage open PRs on {repo} and merge "
                    "every approved, green, mergeable one.",
                }
            ]
        },
        config={"recursion_limit": 30},
    )
    calls = sum(
        len(getattr(m, "tool_calls", []) or [])
        for m in result["messages"]
    )
    outcomes = [
        str(m.content).strip().splitlines()[0][:200]
        for m in result["messages"]
        if m.__class__.__name__ == "ToolMessage"
        and getattr(m, "name", "") == "merge_pr"
        and str(m.content).strip()
    ]
    ledger.record(
        "merge_approved.py",
        f"ollama:{MODEL_ID}",
        thread_id,
        result["messages"],
        started,
    )
    print(f"tool_calls_made: {calls}")
    print("REPORT (authoritative, from merge_pr tool results):")
    if outcomes:
        for line in outcomes:
            print(f"  {line}")
    else:
        print("  (no merge attempted)")
    print("FINAL:", result["messages"][-1].content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
