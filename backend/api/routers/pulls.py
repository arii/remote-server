"""pulls router: PR read + action endpoints.

Ports of the buttons/server.py handlers, same verbs under the
backend's /api prefix convention (buttons serves /buttons/api/*;
this backend serves /api/* like its sibling routers):

- GET  /api/prs?repo=            open PRs, shaped rows
- GET  /api/pr-details?repo=&pr= enriched details + AI prompts
- GET  /api/pr-diff?repo=&pr=    filtered unified diff, truncated
- POST /api/pr-action             merge / update / approve /
                                  request-changes with gating
                                  (fix-ci retired, #428)

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
from backend.pr_gates import (FAILING_CONCLUSIONS, ci_label,
                              evaluate_merge_gates, merge_argv)
from core_cli import context_filter

router = APIRouter(prefix="/api", tags=["pulls"])

PR_LIST_FIELDS = ("number,title,isDraft,mergeable,reviewDecision,"
                  "headRefName,statusCheckRollup,url,author,updatedAt")
PR_VIEW_FIELDS = ("number,title,headRefName,baseRefName,mergeable,"
                  "mergeStateStatus,reviewDecision,statusCheckRollup,"
                  "updatedAt,author,isDraft,url,files")

# fix-ci was a fifth member until #428: it shelled to
# `td agent fix-ci --execute`, a dead backend dispatch.
PR_ACTIONS = ("merge", "update", "approve",
              "request-changes")

# Max chars for a request-changes review body (#616).
REVIEW_BODY_MAX = 2000


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
    # Backend follow-up for #817: the Review sort control orders rows
    # by updatedAt; extra="allow" would pass it through undeclared,
    # but the named field documents the contract the UI relies on.
    updatedAt: str = ""
    # #848: True when GitHub reports any review decision for the PR
    # (APPROVED / CHANGES_REQUESTED / REVIEW_REQUIRED). A blank
    # decision means no required-review rule is in play, so the
    # console merge gate must not demand an approval GitHub itself
    # would never require. Defaults strict for older payloads.
    reviewRequired: bool = True


class PrList(BaseModel):
    repo: str = ""
    prs: list = Field(default_factory=list)
    # #848: login gh authenticates as. The UI compares it to each
    # row's author to disable Approve on self-authored PRs with an
    # inline reason. Blank when unknown: Approve stays enabled and
    # the endpoint's readable 400 is the fallback.
    viewer: str = ""


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
    # Review text for request-changes only; ignored otherwise.
    body: str = ""
    # Explicit branch for the action (#708): validated against the
    # checkout when present; blank behaves exactly as today.
    branch: str = ""


class PrActionResult(BaseModel):
    ok: bool = True
    output: str = ""


# NOTE: FAILING_CONCLUSIONS + ci_label live in backend/pr_gates.py
# (single-sourced with the scheduled gated-merge path) and are
# imported above; list/details paths use them unchanged.


def _gh(runner=None):
    """Injectable subprocess module for tests (None = real)."""
    return runner or subprocess


def _gh_viewer(runner=None) -> str:
    """Login gh authenticates as (#848 self-approval signal).

    Runs `gh api user` directly instead of via run_gh: the viewer
    endpoint is account-scoped, and run_gh's #544 guard requires a
    --repo argv the call has no use for. Fail-open: any failure
    returns "" and the UI keeps Approve enabled (the endpoint's
    readable 400 stays the fallback)."""
    run = _gh(runner)
    try:
        proc = run.run(
            ["gh", "api", "user", "--jq", ".login"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            text=True,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    out = (proc.stdout or "").strip()
    if len(out) >= 2 and out.startswith('"') and out.endswith('"'):
        out = out[1:-1]
    if not out or "/" in out or any(c.isspace() for c in out):
        return ""
    return out


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
        review_decision = str(pr.get("reviewDecision") or "")
        rows.append({
            "number": pr.get("number"),
            "title": pr.get("title", ""),
            "author": author,
            "branch": pr.get("headRefName", ""),
            "isDraft": bool(pr.get("isDraft")),
            "ciStatus": label,
            "ciLabel": label,
            "reviewDecision": review_decision,
            # #848: any reported decision means reviews are in play
            # for this PR; blank means no required-review rule.
            "reviewRequired": review_decision != "",
            "mergeable": str(pr.get("mergeable") or "UNKNOWN").upper(),
            "url": pr.get("url", ""),
            "updatedAt": pr.get("updatedAt") or "",
        })
    return {"repo": repo, "prs": rows, "viewer": _gh_viewer(runner)}


PR_VIEW_GATE_FIELDS = ("statusCheckRollup,mergeStateStatus,"
                         "reviewDecision")


def _pr_view(gh: str, pr_number: int, runner=None) -> tuple:
    """Fresh (CI label, merge state, review decision) for gating."""
    view = run_gh_json(gh, ["pr", "view", str(pr_number), "--repo", gh,
                            "--json", PR_VIEW_GATE_FIELDS],
                       timeout=60, runner=_gh(runner))
    return (ci_label(view.get("statusCheckRollup")),
            str(view.get("mergeStateStatus") or "?").lower(),
            str(view.get("reviewDecision") or ""))


def _pr_action(gh: str, pr_number: int, action: str,
               runner=None, body: str = "") -> dict:
    """Port of buttons/server.py::_pr_action: gating + gh/td argv.
    ValueError = gate refused (400)."""
    run = _gh(runner)
    if action == "approve":
        # No CI/merge gate: a review is valid whatever the checks say.
        # --repo is explicit: #544 showed an unscoped verb resolves
        # against the backend cwd, not the gated repo.
        try:
            out = run_gh(gh, ["pr", "review", str(pr_number),
                              "--repo", gh, "--approve"],
                         runner=run)
        except RuntimeError as exc:
            # #848: GitHub forbids self-approval ("Can not approve
            # your own pull request"). Translate to a gate refusal
            # (400 with a human message) instead of an opaque 500.
            if "approve your own" in str(exc).lower():
                raise ValueError(
                    "GitHub forbids approving your own PR — ask a "
                    "teammate for a review, or merge directly if the "
                    "repo allows it")
            raise
        return {"ok": True, "output": out[-2000:]}
    if action == "request-changes":
        if not body or not body.strip():
            raise ValueError(
                "request-changes requires a review body")
        if len(body) > REVIEW_BODY_MAX:
            raise ValueError(
                f"review body too long (max {REVIEW_BODY_MAX} chars)")
        out = run_gh(gh, ["pr", "review", str(pr_number),
                          "--repo", gh, "--request-changes",
                          "-b", body],
                     runner=run)
        return {"ok": True, "output": out[-2000:]}
    if action == "merge":
        view = run_gh_json(gh, ["pr", "view", str(pr_number),
                                "--repo", gh,
                                "--json", PR_VIEW_GATE_FIELDS],
                           timeout=60, runner=run)
        ok, fails, facts = evaluate_merge_gates(view)
        if not ok:
            gates = {gate for gate, _, _ in fails}
            if gates & {"ci", "merge"}:
                raise ValueError(
                    f"not mergeable: CI {facts['ci']}, "
                    f"merge {facts['merge']}")
            # #848: demand an approval only when the target repo does.
            # REVIEW_REQUIRED is GitHub's own required-but-missing signal;
            # CHANGES_REQUESTED is an explicit human veto. A blank decision
            # means no required-review rule — fail open and let `gh pr
            # merge` (which enforces the repo's real rules) decide.
            if facts["review"] == "REVIEW_REQUIRED":
                raise ValueError(
                    "not mergeable: approval required by branch rules "
                    "(review is REVIEW_REQUIRED)")
            raise ValueError(
                "not mergeable: changes were requested on this PR")
        out = run_gh(gh, merge_argv(gh, pr_number), runner=run)
        return {"ok": True, "output": out[-2000:]}
    if action == "update":
        _ci, merge, _review = _pr_view(gh, pr_number, run)
        if merge != "behind":
            raise ValueError(f"cannot update: merge state is {merge} "
                             "(only behind branches update this way)")
        out = run_gh(gh, ["api",
                          f"repos/{gh}/pulls/{pr_number}/update-branch",
                          "--method", "PUT"], runner=run)
        return {"ok": True, "output": out[-2000:]}
    # pr_action allowlists before calling; direct callers with any
    # other action (including retired fix-ci, #428) fail here.
    raise ValueError(f"unknown action: {action}")


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


class HiddenStructural(BaseModel):
    count: int = 0
    files: list = Field(default_factory=list)


class PrDiff(BaseModel):
    number: int = 0
    diff: str = ""
    truncated: bool = False
    hidden_structural: HiddenStructural = Field(
        default_factory=HiddenStructural)


@router.get("/pr-diff", response_model=PrDiff)
def pr_diff(repo: str = "remote-server", pr: int = 0,
            runner=None) -> dict:
    try:
        gh = repo_gh(repo)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad repo")
    if (isinstance(pr, bool) or not isinstance(pr, int)
            or pr <= 0):
        raise HTTPException(status_code=400, detail="invalid pr")
    try:
        raw = run_gh(gh, ["pr", "diff", str(pr), "--repo", gh],
                     timeout=60, runner=runner)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:500])
    diff = context_filter.filter_diff(raw, max_file_lines=120)
    hidden = context_filter.structural_hidden_files(raw or "")
    shape = {"count": len(hidden), "files": hidden}
    if len(diff) > 30000:
        return {"number": pr, "diff": diff[:30000], "truncated": True,
                "hidden_structural": shape}
    return {"number": pr, "diff": diff, "truncated": False,
            "hidden_structural": shape}


@router.post("/pr-action", response_model=PrActionResult)
def pr_action(body: PrActionRequest) -> dict:
    from backend.core import require_confirm

    # Retired (#428): fix-ci shelled to `td agent fix-ci
    # --execute`, whose backend is dead. Named explicitly (not
    # folded into the generic bad action error) so stale consoles
    # learn why the tap fails.
    if body.action == "fix-ci":
        raise HTTPException(
            status_code=400,
            detail=("fix-ci was retired (#428): the CI-fix backend "
                    "is dead; file an issue and dispatch the repair "
                    "worker from Jobs, or push a fix to the branch"))
    if body.action == "merge":
        require_confirm(body, "pr-merge")
    elif body.action == "approve":
        require_confirm(body, "pr-approve")
    elif body.action == "request-changes":
        require_confirm(body, "pr-request-changes")
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
    from backend.core import validate_action_branch

    try:
        validate_action_branch(body.repo, body.branch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        return _pr_action(gh, body.pr_number, body.action,
                          body=body.body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:500])
