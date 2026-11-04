"""issues router: issue read/write + repair worker endpoints.

Ports of the buttons/server.py handlers, same verbs under the
backend's /api prefix convention (buttons serves /buttons/api/*;
this backend serves /api/* like its sibling routers):

- GET  /api/issues?repo=          open issues, body truncated
- POST /api/create-issue          create an issue (confirm-tap shape)
- POST /api/issue-draft           build a structured issue draft
- GET  /api/labels?repo=           repo label names (typeahead source)
- POST /api/issue-label            add/remove one label on an issue
- POST /api/repair/dispatch       start the repair worker for an issue
- GET  /api/repair/status         repair worker state (+ schedules)
- POST /api/repair/abort          abort the in-flight repair worker
- GET  /api/repair/verify-command parsed verify cmd + allowlist verdict
- POST /api/repair/schedule       set/clear a daily/weekly repair schedule

Repair runs the worker the same way the legacy handler does: the
same argv (python3 -m core_cli repair --issue N) with no new
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
from core_cli.parse_task_spec import parse_task_spec
from core_cli.verify_allowlist import (
    VERIFY_VERB_HELP,
    validate_verify_command,
)

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
# In-flight worker proc + abort flag, guarded by repair_state_lock.
_repair_proc = None
_repair_abort = False

# Per-issue repair schedules: {issue_number: {"cadence": "daily"|"weekly",
# "next_run": epoch_seconds, "last_run": epoch_seconds|None}}.
# In-memory only (no new infra): schedules do not survive restarts.
# Guarded by repair_state_lock. A daemon ticker fires due schedules
# when the worker is idle; repair_status() also picks up due ones.
SCHEDULE_INTERVALS = {"daily": 24 * 3600, "weekly": 7 * 24 * 3600}
SCHEDULE_TICK_SECONDS = 60
repair_schedules: dict = {}
_schedule_ticker = None


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


class LabelList(BaseModel):
    repo: str = ""
    labels: list = Field(default_factory=list)


class IssueLabelRequest(BaseModel):
    repo: str = "remote-server"
    issue: object = None
    label: str = ""
    op: str = ""


class IssueLabelResult(BaseModel):
    ok: bool = True
    issue: int = 0


LABEL_OPS = ("add", "remove")
LABEL_NAME_MAX = 100


def parse_label_name(val) -> str | None:
    """Trimmed label name, or None when missing/blank/too long.

    GitHub label names allow almost any characters, so only the
    shape is validated here; gh enforces that the label exists
    (unknown names fail the edit, surfaced as a 500 like other
    gh failures in this router).
    """
    if not isinstance(val, str):
        return None
    name = val.strip()
    if not name or len(name) > LABEL_NAME_MAX:
        return None
    return name


class RepairDispatchRequest(BaseModel):
    issue: object = None
    # Explicit branch for the dispatch (#708): validated against
    # the stack checkout when present; blank behaves as today.
    branch: str = ""


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
    schedules: list = Field(default_factory=list)


class RepairSchedule(BaseModel):
    issue: int = 0
    cadence: str = ""
    next_run: float = 0.0
    last_run: float | None = None


class RepairScheduleRequest(BaseModel):
    issue: object = None
    cadence: str = ""


class RepairScheduleResult(BaseModel):
    ok: bool = True
    schedule: RepairSchedule | None = None


def _repair_worker_thread(issue_number: int) -> None:
    """Run the repair worker, same argv as the legacy handler, and
    record state and logs. Never raises out."""
    global _repair_proc, _repair_abort
    with repair_state_lock:
        repair_state["status"] = "running"
        repair_state["timestamp"] = time.time()
        repair_state["issue"] = issue_number
        repair_state["log_buffer"] = (
            f"Dispatched repair worker for issue #{issue_number}\n")
        repair_state["exit_code"] = None
        _repair_proc = None
        _repair_abort = False
    run = _repair_runner or subprocess
    cmd = ["python3", "-m", "core_cli", "repair",
           "--issue", str(issue_number)]
    try:
        # cwd = live checkout: the worker path is repo-relative and the
        # image workdir (/app) has no scripts/ dir.
        proc = run.Popen(cmd, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, bufsize=1,
                         cwd=stack_dir())
        with repair_state_lock:
            _repair_proc = proc
            spawned_aborted = _repair_abort
        if spawned_aborted:
            # Abort landed during spawn: stop immediately.
            try:
                proc.terminate()
            except Exception:
                pass
        if proc.stdout:
            for line in proc.stdout:
                with repair_state_lock:
                    repair_state["log_buffer"] += line
                    if len(repair_state["log_buffer"]) > MAX_OUTPUT:
                        repair_state["log_buffer"] = \
                            repair_state["log_buffer"][-MAX_OUTPUT:]
        code = proc.wait()
        with repair_state_lock:
            _repair_proc = None
            if _repair_abort and code is not None and code < 0:
                # Abort only counts when it killed the proc (signal
                # exit); a natural exit after a late abort stays done.
                repair_state["status"] = "aborted"
                repair_state["exit_code"] = None
                repair_state["log_buffer"] += "\nAborted by user.\n"
            else:
                repair_state["status"] = "done" if code == 0 else "failed"
                repair_state["exit_code"] = code
                repair_state["log_buffer"] += (
                    f"\nProcess exited with code {code}\n")
    except Exception as exc:
        with repair_state_lock:
            _repair_proc = None
            repair_state["status"] = "failed"
            repair_state["exit_code"] = 1
            repair_state["log_buffer"] += (
                f"\nFailed to spawn repair worker: {exc}\n")


class RepairAbortResult(BaseModel):
    ok: bool = True
    killed: bool = False


class RepairVerifyPreview(BaseModel):
    ok: bool = True
    issue: int = 0
    command: str = ""
    allowlisted: bool = False
    error: str = ""


def _repair_verify_preview(issue_number: int) -> dict:
    """Parsed verification command + allowlist verdict for an issue.

    Shared by the preview endpoint (the confirmer sees the command)
    and dispatch (refuse to spawn rejected commands). Repair targets
    the stack repo only — same scope as the worker argv.
    """
    try:
        gh = repo_gh("remote-server")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        row = run_gh_json(gh, ["issue", "view", str(issue_number),
                               "--repo", gh, "--json", "body"],
                          timeout=60)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:500])
    body = row.get("body", "") if isinstance(row, dict) else ""
    try:
        cmd = parse_task_spec(body or "").get("verification_command", "")
    except ValueError as exc:
        return {"ok": True, "issue": issue_number, "command": "",
                "allowlisted": False, "error": str(exc)[:300]}
    if not validate_verify_command(cmd):
        return {"ok": True, "issue": issue_number, "command": cmd[:500],
                "allowlisted": False,
                "error": f"not allowlisted (allowed: {VERIFY_VERB_HELP})"}
    return {"ok": True, "issue": issue_number, "command": cmd[:500],
            "allowlisted": True, "error": ""}


def trigger_repair(issue_number: int):
    t = threading.Thread(target=_repair_worker_thread,
                         args=(issue_number,), daemon=True)
    t.start()
    return t


def _claim_due_schedule(now: float) -> int | None:
    """Claim one due schedule when the worker is idle.

    Advances the claimed schedule's next_run before returning so a
    firing is never double-claimed. Returns the issue number, or
    None when nothing is due or the worker is busy.
    """
    with repair_state_lock:
        if repair_state["status"] == "running":
            return None
        for issue_number in sorted(repair_schedules):
            sched = repair_schedules[issue_number]
            if sched["next_run"] <= now:
                sched["last_run"] = now
                sched["next_run"] = (
                    now + SCHEDULE_INTERVALS[sched["cadence"]])
                return issue_number
    return None


def fire_due_schedules(now: float | None = None) -> int | None:
    """Fire one due schedule through the same gate as one-shot
    dispatch. Never raises out of the ticker. Returns the fired
    issue number, or None."""
    claimed = _claim_due_schedule(now if now is not None
                                  else time.time())
    if claimed is None:
        return None
    try:
        preview = _repair_verify_preview(claimed)
    except Exception:
        return None
    if not preview["allowlisted"]:
        return None
    trigger_repair(claimed)
    return claimed


def _schedule_tick_loop() -> None:
    while True:
        time.sleep(SCHEDULE_TICK_SECONDS)
        try:
            fire_due_schedules()
        except Exception:
            pass
        # Shared tick (see #636): scheduled digests ride this loop
        # rather than running a second ticker. Repair-owned loop;
        # digest firing lives in backend.notify.
        try:
            from backend.notify import fire_due_digest
            fire_due_digest()
        except Exception:
            pass
        # Third job on the shared tick (#652): budget-watch alerts
        # ride this loop like digests — never a second ticker.
        try:
            from backend.notify import fire_budget_watch
            fire_budget_watch()
        except Exception:
            pass
        # Fourth job on the shared tick (#655): scheduled jobs
        # actions ride this loop like digests — never a second ticker.
        try:
            from backend.api.routers.jobs import \
                fire_due_action_schedules
            fire_due_action_schedules()
        except Exception:
            pass


def _ensure_schedule_ticker() -> None:
    global _schedule_ticker
    with repair_state_lock:
        if _schedule_ticker is not None:
            return
        _schedule_ticker = threading.Thread(
            target=_schedule_tick_loop, daemon=True)
        _schedule_ticker.start()


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


@router.get("/labels", response_model=LabelList)
def repo_labels(repo: str = "remote-server") -> dict:
    try:
        gh = repo_gh(repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        rows = run_gh_json(gh, ["label", "list", "--repo", gh,
                                "--limit", "100", "--json", "name"],
                           timeout=60)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:500])
    names = sorted({r.get("name", "") for r in rows
                    if isinstance(r, dict)
                    and isinstance(r.get("name"), str)
                    and r.get("name", "").strip()})
    return {"repo": repo, "labels": names}


@router.post("/issue-label", response_model=IssueLabelResult)
def issue_label(body: IssueLabelRequest) -> dict:
    issue_number = parse_issue_number(body.issue)
    if issue_number is None:
        raise HTTPException(
            status_code=400, detail="invalid issue number or url")
    label = parse_label_name(body.label)
    if body.op not in LABEL_OPS or label is None:
        raise HTTPException(
            status_code=400,
            detail="op must be add or remove with a label name")
    try:
        gh = repo_gh(body.repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    flag = "--add-label" if body.op == "add" else "--remove-label"
    try:
        run_gh(gh, ["issue", "edit", str(issue_number),
                    "--repo", gh, flag, label], timeout=60)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:500])
    return {"ok": True, "issue": issue_number}


@router.get("/repair/verify-command",
            response_model=RepairVerifyPreview)
def repair_verify_command(issue: str = "") -> dict:
    issue_number = parse_issue_number(issue)
    if issue_number is None:
        raise HTTPException(
            status_code=400, detail="invalid issue number or url")
    return _repair_verify_preview(issue_number)


@router.post("/repair/dispatch", response_model=RepairDispatchResult)
def repair_dispatch(body: RepairDispatchRequest) -> dict:
    issue_number = parse_issue_number(body.issue)
    if issue_number is None:
        raise HTTPException(
            status_code=400, detail="invalid issue number or url")
    from backend.core import validate_action_branch

    # Stack scope, like the worker argv and the verify preview.
    try:
        validate_action_branch("remote-server", body.branch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    preview = _repair_verify_preview(issue_number)
    if not preview["allowlisted"]:
        raise HTTPException(
            status_code=400,
            detail=preview["error"] or "verification command rejected")
    trigger_repair(issue_number)
    return {"ok": True, "issue": issue_number, "status": "running"}


@router.post("/repair/schedule",
            response_model=RepairScheduleResult)
def repair_schedule(body: RepairScheduleRequest) -> dict:
    issue_number = parse_issue_number(body.issue)
    if issue_number is None:
        raise HTTPException(
            status_code=400, detail="invalid issue number or url")
    cadence = (body.cadence or "").strip().lower()
    if cadence == "off":
        with repair_state_lock:
            repair_schedules.pop(issue_number, None)
        return {"ok": True, "schedule": None}
    if cadence not in SCHEDULE_INTERVALS:
        raise HTTPException(
            status_code=400,
            detail="cadence must be daily, weekly, or off")
    now = time.time()
    with repair_state_lock:
        prev = repair_schedules.get(issue_number, {})
        sched = {"cadence": cadence,
                 "next_run": now + SCHEDULE_INTERVALS[cadence],
                 "last_run": prev.get("last_run")}
        repair_schedules[issue_number] = sched
    _ensure_schedule_ticker()
    return {"ok": True, "schedule": {"issue": issue_number, **sched}}


@router.get("/repair/status", response_model=RepairStatus)
def repair_status() -> dict:
    fire_due_schedules()
    with repair_state_lock:
        out = dict(repair_state)
        out["schedules"] = [
            {"issue": n, **repair_schedules[n]}
            for n in sorted(repair_schedules)]
        return out


@router.post("/repair/abort", response_model=RepairAbortResult)
def repair_abort() -> dict:
    """Abort the in-flight repair worker, if any. Mirrors the agent
    turn abort: flag first (the worker records "aborted", not an
    error), then terminate with a kill fallback."""
    global _repair_abort
    with repair_state_lock:
        if repair_state["status"] != "running":
            return {"ok": True, "killed": False}
        _repair_abort = True
        proc = _repair_proc
    if proc is None:
        return {"ok": True, "killed": False}
    try:
        if proc.poll() is not None:
            # Already exited on its own: nothing to kill, so clear the
            # flag set above instead of poisoning the status write.
            with repair_state_lock:
                _repair_abort = False
            return {"ok": True, "killed": False}
    except Exception:
        return {"ok": True, "killed": False}
    try:
        proc.terminate()
        proc.wait(5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    return {"ok": True, "killed": True}
