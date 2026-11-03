"""issues router: issue read/write + repair worker endpoints.

Ports of the buttons/server.py handlers, same verbs under the
backend's /api prefix convention (buttons serves /buttons/api/*;
this backend serves /api/* like its sibling routers):

- GET  /api/issues?repo=          open issues, body truncated
- POST /api/create-issue          create an issue (confirm-tap shape)
- POST /api/issue-draft           build a structured issue draft
- POST /api/repair/dispatch       start the repair worker for an issue
- GET  /api/repair/status         repair worker state

Repair runs the worker the same way the legacy handler does: the
same argv (python3 scripts/repair_worker.py --issue N) with no new
capabilities. No labeled-event wiring here (later follow-up, and
no notify.py changes in this port). No Jules dispatch surface.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.exec.engine import stack_dir
from backend.gh import repo_gh, run_gh, run_gh_json

router = APIRouter(prefix="/api", tags=["issues"])

ISSUE_LIST_FIELDS = ("number,title,state,assignees,labels,updatedAt,"
                     "url,body")
MAX_OUTPUT = 200_000

repair_state = {
    "status": "idle",
    "timestamp": None,
    "log_buffer": "",
    "issue": None,
    "exit_code": None,
}
repair_state_lock = threading.Lock()

_repair_runner = None


def parse_issue_number(val) -> int | None:
    """Port of buttons/server.py::_parse_issue_number: int, string
    ('123', '#123'), or issue URL. None when unparseable."""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val if val > 0 else None
    if isinstance(val, str):
        val = val.strip()
        if not val:
            return None
        if val.startswith("#"):
            val = val[1:].strip()
        if val.isdigit():
            n = int(val)
            return n if n > 0 else None
        m = re.search(r"/issues/(\d+)(?:[/?#]|$)", val)
        if m:
            n = int(m.group(1))
            return n if n > 0 else None
    return None


class IssueList(BaseModel):
    repo: str = ""
    issues: list = Field(default_factory=list)


class CreateIssueRequest(BaseModel):
    repo: str = "remote-server"
    title: str = ""
    body: str = ""


class CreateIssueResult(BaseModel):
    ok: bool = True
    url: str = ""


class IssueDraftRequest(BaseModel):
    repo: str = "remote-server"
    source: str = "finding"
    title: str = "Issue report from findings"
    cause: str = ""
    details: str = ""


class IssueDraft(BaseModel):
    ok: bool = True
    title: str = ""
    body: str = ""


class RepairDispatchRequest(BaseModel):
    issue: object = None


class RepairDispatchResult(BaseModel):
    ok: bool = True
    issue: int = 0
    status: str = "running"


class RepairStatus(BaseModel):
    status: str = "idle"
    timestamp: float | None = None
    log_buffer: str = ""
    issue: int | None = None
    exit_code: int | None = None


def _repair_worker_thread(issue_number: int) -> None:
    """Run the repair worker, same argv as the legacy handler, and
    record state and logs. Never raises out."""
    with repair_state_lock:
        repair_state["status"] = "running"
        repair_state["timestamp"] = time.time()
        repair_state["issue"] = issue_number
        repair_state["log_buffer"] = (
            f"Dispatched repair worker for issue #{issue_number}\n")
        repair_state["exit_code"] = None
    run = _repair_runner or subprocess
    cmd = ["python3", "scripts/repair_worker.py",
           "--issue", str(issue_number)]
    try:
        # cwd = live checkout: the worker path is repo-relative and the
        # image workdir (/app) has no scripts/ dir.
        proc = run.Popen(cmd, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, bufsize=1,
                         cwd=stack_dir())
        if proc.stdout:
            for line in proc.stdout:
                with repair_state_lock:
                    repair_state["log_buffer"] += line
                    if len(repair_state["log_buffer"]) > MAX_OUTPUT:
                        repair_state["log_buffer"] = \
                            repair_state["log_buffer"][-MAX_OUTPUT:]
        code = proc.wait()
        with repair_state_lock:
            repair_state["status"] = "done" if code == 0 else "failed"
            repair_state["exit_code"] = code
            repair_state["log_buffer"] += f"\nProcess exited with code {code}\n"
    except Exception as exc:
        with repair_state_lock:
            repair_state["status"] = "failed"
            repair_state["exit_code"] = 1
            repair_state["log_buffer"] += (
                f"\nFailed to spawn repair worker: {exc}\n")


def trigger_repair(issue_number: int):
    t = threading.Thread(target=_repair_worker_thread,
                         args=(issue_number,), daemon=True)
    t.start()
    return t


@router.get("/issues", response_model=IssueList)
def issues(repo: str = "remote-server") -> dict:
    try:
        gh = repo_gh(repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        rows = run_gh_json(gh, ["issue", "list", "--repo", gh,
                                "--state", "open", "--limit", "50",
                                "--json", ISSUE_LIST_FIELDS], timeout=60)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:500])
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get("body"), str) \
                and len(row["body"]) > 600:
            row["body"] = row["body"][:600] + "…"
    return {"repo": repo, "issues": rows}


@router.post("/create-issue", response_model=CreateIssueResult)
def create_issue(body: CreateIssueRequest) -> dict:
    try:
        gh = repo_gh(body.repo)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="bad repo, title or body")
    if (not isinstance(body.title, str) or not body.title.strip()
            or len(body.title) > 200
            or not isinstance(body.body, str)
            or len(body.body) > 4000):
        raise HTTPException(
            status_code=400, detail="bad repo, title or body")
    try:
        out = run_gh(gh, ["issue", "create", "--repo", gh,
                          "--title", body.title.strip(),
                          "--body", body.body.strip()], timeout=120)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:500])
    lines = out.strip().splitlines()
    return {"ok": True, "url": (lines[-1] if lines else "")[:500]}


@router.post("/issue-draft", response_model=IssueDraft)
def issue_draft(body: IssueDraftRequest) -> dict:
    try:
        repo_gh(body.repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    md = (f"### Summary\nReported from {body.source} findings on "
          f"{body.repo}.\n\n")
    if body.cause:
        md += f"### Likely Cause / Finding\n{body.cause}\n\n"
    if body.details:
        md += f"### Details & Context\n```\n{body.details}\n```\n\n"
    md += ("### Recommended Remediation\n- [ ] Investigate root cause\n"
           "- [ ] Apply automated or manual fix\n"
           "- [ ] Verify test suite passes\n")
    return {"ok": True, "title": body.title, "body": md}


@router.post("/repair/dispatch", response_model=RepairDispatchResult)
def repair_dispatch(body: RepairDispatchRequest) -> dict:
    issue_number = parse_issue_number(body.issue)
    if issue_number is None:
        raise HTTPException(
            status_code=400, detail="invalid issue number or url")
    trigger_repair(issue_number)
    return {"ok": True, "issue": issue_number, "status": "running"}


@router.get("/repair/status", response_model=RepairStatus)
def repair_status() -> dict:
    with repair_state_lock:
        return dict(repair_state)
