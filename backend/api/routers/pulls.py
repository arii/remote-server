"""pulls router: PR read + action endpoints.

Ports of the buttons/server.py handlers, same verbs under the
backend's /api prefix convention (buttons serves /buttons/api/*;
this backend serves /api/* like its sibling routers):

- GET  /api/prs?repo=            open PRs, shaped rows
- GET  /api/pr-details?repo=&pr= enriched details + AI prompts
- GET  /api/pr-diff?repo=&pr=    filtered unified diff, truncated
- POST /api/pr-action             merge / update / fix-ci with gating

Note: the legacy /buttons/api/prs handler calls _format_ci_status
and _format_review_decision, which are not defined anywhere in
buttons/ — that path raises NameError at runtime. Here both CI
fields come from the ci_summary label (the same _ci_label the
legacy code uses for ciLabel), and reviewDecision passes through
as a string.
"""

from __future__ import annotations

import subprocess

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from backend.gh import repo_gh, run_gh, run_gh_json
from core_cli import context_filter

router = APIRouter(prefix="/api", tags=["pulls"])

PR_LIST_FIELDS = ("number,title,isDraft,mergeable,reviewDecision,"
                  "headRefName,statusCheckRollup,url,author")
PR_VIEW_FIELDS = ("number,title,headRefName,baseRefName,mergeable,"
                  "mergeStateStatus,reviewDecision,statusCheckRollup,"
                  "updatedAt,author,isDraft,url,files")

PR_ACTIONS = ("merge", "update", "fix-ci")


class PrRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    number: int
    title: str = ""
    author: str = ""
    branch: str = ""
    isDraft: bool = False
    ciStatus: str = "unknown"
    ciLabel: str = "unknown"
    reviewDecision: str = ""
    mergeable: str = "UNKNOWN"
    url: str = ""


class PrList(BaseModel):
    repo: str = ""
    prs: list = Field(default_factory=list)


class PrDetails(BaseModel):
    model_config = ConfigDict(extra="allow")

    repo: str = ""
    number: int = 0
    title: str = ""
    branch: str = ""
    base: str = ""
    ci: str = "unknown"
    failing: list = Field(default_factory=list)
    merge: str = "?"
    conflicted: bool = False
    draft: bool = False
    url: str = ""
    files: list = Field(default_factory=list)
    stats: dict = Field(default_factory=dict)
    ai_prompts: list = Field(default_factory=list)


class PrActionRequest(BaseModel):
    # pr_number is object (not int) so JSON bools arrive uncoerced:
    # like the legacy handler, True must be rejected, not read as 1.
    repo: str = "remote-server"
    pr_number: object = 0
    action: str = ""
    intent: str = ""
    confirmed: bool = False


class PrActionResult(BaseModel):
    ok: bool = True
    output: str = ""


FAILING_CONCLUSIONS = {"FAILURE", "CANCELLED", "TIMED_OUT",
                         "ACTION_REQUIRED"}


def ci_label(rollup) -> str:
    """Port of core_cli/briefing_summary.py::ci_summary label."""
    if rollup is None:
        return "unknown"
    if not rollup:
        return "no checks"
    if any(isinstance(c, dict) and c.get("conclusion")
           in FAILING_CONCLUSIONS for c in rollup):
        return "failing"
    if any(isinstance(c, dict) and c.get("status") != "COMPLETED"
           for c in rollup):
        return "pending"
    return "passing"


def _gh(runner=None):
    """Injectable subprocess module for tests (None = real)."""
    return runner or subprocess


@router.get("/prs", response_model=PrList)
def prs(repo: str = "remote-server", runner=None) -> dict:
    try:
        gh = repo_gh(repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        raw = run_gh_json(gh, ["pr", "list", "--repo", gh,
                               "--state", "open", "--limit", "50",
                               "--json", PR_LIST_FIELDS],
                          timeout=60, runner=_gh(runner))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:500])
    rows = []
    for pr in raw if isinstance(raw, list) else []:
        if not isinstance(pr, dict):
            continue
        author_val = pr.get("author")
        author = (author_val.get("login")
                  if isinstance(author_val, dict)
                  else (author_val or ""))
        label = ci_label(pr.get("statusCheckRollup"))
        rows.append({
            "number": pr.get("number"),
            "title": pr.get("title", ""),
            "author": author,
            "branch": pr.get("headRefName", ""),
            "isDraft": bool(pr.get("isDraft")),
            "ciStatus": label,
            "ciLabel": label,
            "reviewDecision": str(pr.get("reviewDecision") or ""),
            "mergeable": str(pr.get("mergeable") or "UNKNOWN").upper(),
            "url": pr.get("url", ""),
        })
    return {"repo": repo, "prs": rows}


def _pr_view(gh: str, pr_number: int, runner=None) -> tuple:
    """Fresh (CI label, merge state) for gating, at tap time."""
    view = run_gh_json(gh, ["pr", "view", str(pr_number), "--repo", gh,
                            "--json", "statusCheckRollup,mergeStateStatus"],
                       timeout=60, runner=_gh(runner))
    return (ci_label(view.get("statusCheckRollup")),
            str(view.get("mergeStateStatus") or "?").lower())


def _pr_action(gh: str, pr_number: int, action: str,
               runner=None) -> dict:
    """Port of buttons/server.py::_pr_action: gating + gh/td argv.
    ValueError = gate refused (400)."""
    run = _gh(runner)
    if action == "merge":
        ci, merge = _pr_view(gh, pr_number, run)
        if ci != "passing" or merge != "clean":
            raise ValueError(f"not mergeable: CI {ci}, merge {merge}")
        out = run_gh(gh, ["pr", "merge", str(pr_number),
                          "--squash", "--delete-branch"],
                     runner=run)
        return {"ok": True, "output": out[-2000:]}
    if action == "update":
        _ci, merge = _pr_view(gh, pr_number, run)
        if merge != "behind":
            raise ValueError(f"cannot update: merge state is {merge} "
                             "(only behind branches update this way)")
        out = run_gh(gh, ["api",
                          f"repos/{gh}/pulls/{pr_number}/update-branch",
                          "--method", "PUT"], runner=run)
        return {"ok": True, "output": out[-2000:]}
    ci, _merge = _pr_view(gh, pr_number, run)
    if ci != "failing":
        raise ValueError(f"CI is {ci}, not failing — nothing to fix")
    proc = run.run(["td", "--json", "agent", "fix-ci",
                    "--pr", str(pr_number), "--execute"],
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                   timeout=120, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            (proc.stderr or "").strip().splitlines()[-1]
            if (proc.stderr or "").strip() else "fix-ci failed")
    return {"ok": True, "output": proc.stdout[-2000:]}


@router.get("/pr-details", response_model=PrDetails)
def pr_details(repo: str = "remote-server", pr: int = 0,
               runner=None) -> dict:
    try:
        gh = repo_gh(repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if isinstance(pr, bool) or pr <= 0:
        raise HTTPException(status_code=400, detail="invalid pr")
    try:
        view = run_gh_json(gh, ["pr", "view", str(pr), "--repo", gh,
                                "--json", PR_VIEW_FIELDS],
                           timeout=45, runner=_gh(runner))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:500])
    rollup = view.get("statusCheckRollup")
    label = ci_label(rollup)
    failing = [c.get("name", "?") for c in (rollup or [])
               if isinstance(c, dict)
               and c.get("conclusion") in FAILING_CONCLUSIONS]
    merge = str(view.get("mergeStateStatus") or "?").lower()
    files = [{"path": rf.get("path") or "?",
              "additions": rf.get("additions", 0),
              "deletions": rf.get("deletions", 0),
              "changes": rf.get("additions", 0) + rf.get("deletions", 0)}
             for rf in (view.get("files") or [])
             if isinstance(rf, dict)]
    file_list = ", ".join(f["path"] for f in files[:10])
    prompts = [
        {"id": "security", "label": "Security Audit",
         "prompt": f"Audit PR #{pr} for security vulnerabilities, "
                   f"injection risks, and auth flaws across changed "
                   f"files: {file_list}"},
        {"id": "architecture", "label": "Architecture Review",
         "prompt": f"Review design decisions, coupling, and modularity "
                   f"in PR #{pr} ({view.get('title')})."},
        {"id": "discipline", "label": "Code Discipline Review",
         "prompt": f"Check PR #{pr} for workaround chains (e.g. "
                   f"`cmd || variant-cmd`, try/except ladders, or "
                   f"fallback chains papering over failures). A "
                   f"workaround chain is a review-blocking finding, not "
                   f"a nit — flag any instance and demand root-cause "
                   f"fixes."},
        {"id": "test_gaps", "label": "Test Gap Analysis",
         "prompt": f"Determine untested paths and recommend automated "
                   f"tests for PR #{pr}."},
        {"id": "summary", "label": "Review Comments",
         "prompt": f"Synthesize line-level findings and approval "
                   f"readiness for PR #{pr}. Explicitly block on any "
                   f"workaround chains (`cmd || variant-cmd` or fallback "
                   f"ladders)."},
    ]
    return {
        "repo": repo, "number": pr, "title": view.get("title", ""),
        "branch": view.get("headRefName", ""),
        "base": view.get("baseRefName", ""), "ci": label,
        "failing": failing, "merge": merge,
        "conflicted": merge == "dirty",
        "draft": bool(view.get("isDraft")), "url": view.get("url"),
        "files": files,
        "stats": {"total_files": len(files),
                  "additions": sum(f["additions"] for f in files),
                  "deletions": sum(f["deletions"] for f in files)},
        "ai_prompts": prompts,
    }


class PrDiff(BaseModel):
    number: int = 0
    diff: str = ""
    truncated: bool = False


@router.get("/pr-diff", response_model=PrDiff)
def pr_diff(repo: str = "remote-server", pr: int = 0,
            runner=None) -> dict:
    try:
        gh = repo_gh(repo)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad repo")
    if not isinstance(pr, int) or pr <= 0:
        raise HTTPException(status_code=400, detail="bad pr number")
    try:
        raw = run_gh(gh, ["pr", "diff", str(pr), "--repo", gh],
                     timeout=60, runner=runner)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:500])
    diff = context_filter.filter_diff(raw, max_file_lines=120)
    if len(diff) > 30000:
        return {"number": pr, "diff": diff[:30000], "truncated": True}
    return {"number": pr, "diff": diff, "truncated": False}


@router.post("/pr-action", response_model=PrActionResult)
def pr_action(body: PrActionRequest) -> dict:
    from backend.core import require_confirm

    if body.action == "merge":
        require_confirm(body, "pr-merge")
    # Strict validation, same as legacy: int PR number only (bools
    # are ints in Python, so excluded), allowlisted action.
    if (not isinstance(body.pr_number, int)
            or isinstance(body.pr_number, bool)
            or body.pr_number <= 0
            or body.action not in PR_ACTIONS):
        raise HTTPException(
            status_code=400, detail="bad pr_number, action or repo")
    try:
        gh = repo_gh(body.repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        return _pr_action(gh, body.pr_number, body.action)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:500])
