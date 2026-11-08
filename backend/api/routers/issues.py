"""issues router: issue read/write + repair worker endpoints.

Ports of the buttons/server.py handlers, same verbs under the
backend's /api prefix convention (buttons serves /buttons/api/*;
this backend serves /api/* like its sibling routers):

- GET  /api/issues?repo=          open issues, body truncated
  (repo=all rolls up every checkout with per-row repo tags)
- POST /api/create-issue          create an issue with optional labels
- POST /api/issue-draft           build a structured issue draft
- GET  /api/labels?repo=           repo label names (typeahead source)
- POST /api/issue-label            add/remove one label on an issue
- POST /api/repair/dispatch       start the repair worker for an issue
- GET  /api/repair/status         repair worker state (+ schedules)
- POST /api/repair/abort          abort the in-flight repair worker
- GET  /api/repair/verify-command parsed verify cmd + allowlist verdict
- POST /api/repair/schedule       set/clear a daily/weekly repair schedule

Repair runs the worker like the legacy handler does (python3 -m
backend.repair_worker --issue N --repo R). Dispatch is a confirmed
(intent repair-dispatch + confirmed), single-flight (409 when a
worker runs), repo-scoped action; a run holding the slot past
REPAIR_WORKER_TIMEOUT is killed and records failed, never running
forever. No labeled-event wiring here (later follow-up, and
no notify.py changes in this port).
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
from backend.parse_task_spec import parse_task_spec
from backend.verify_allowlist import (
    VERIFY_VERB_HELP,
    validate_verify_command,
)

router = APIRouter(prefix="/api", tags=["issues"])

ISSUE_LIST_FIELDS = ("number,title,state,assignees,labels,updatedAt,"
                     "url,body")
MAX_OUTPUT = 200_000

REPAIR_WORKER_TIMEOUT = 1800  # seconds of wall clock for one repair
# run (stdout drain + wait). The worker's own steps already bound
# themselves (Ollama 300s/query, verify 300s); a run holding the
# single-flight slot past this is stuck: it is terminated, then
# killed, and records failed — never running forever.

repair_state = {
    "status": "idle",
    "timestamp": None,
    "log_buffer": "",
    "issue": None,
    "repo": None,
    "exit_code": None,
    # Titled failure lifted from the worker log (the
    # repair-unscaffolded marker, #908); None when the run
    # succeeded or failed without a titled reason.
    "failure_reason": None,
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
    labels: list = Field(default_factory=list)


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
    # Repo scoping (#435): basename or arii/ path. The branch
    # check, verify preview, and worker gh calls all target it.
    repo: str = "remote-server"
    # Explicit branch for the dispatch (#708): validated against
    # the scoped repo checkout when present; blank behaves as today.
    branch: str = ""
    # Group focus (#850): a task-spec markdown override for one
    # finding group (from the UX audit groups endpoint). Passed
    # to the worker as --body so the session fixes this group
    # only; blank dispatches the whole issue as today.
    focus: str = ""
    # Confirm tap, same shape as pr-action: intent must be
    # "repair-dispatch" with confirmed True.
    intent: str = ""
    confirmed: bool = False


class RepairDispatchResult(BaseModel):
    ok: bool = True
    issue: int = 0
    status: str = "running"
    repo: str = "remote-server"


class RepairStatus(BaseModel):
    status: str = "idle"
    timestamp: float | None = None
    log_buffer: str = ""
    issue: int | None = None
    repo: str | None = None
    exit_code: int | None = None
    failure_reason: str | None = None
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


# Group-focus cap (#850): group specs run 2-4KB; 8KB bounds
# the worker argv while leaving headroom.
REPAIR_FOCUS_MAX = 8000


def _extract_failure_reason(log_buffer: str) -> str | None:
    """Titled worker failure from the merged log, if any.

    The repair worker prints one `repair-unscaffolded: <reason>`
    line when the issue lacks the scaffold (#908); the last such
    line is the queue-row detail. None when the log carries no
    titled reason (plain exit-code detail, as before). Capped so
    a hostile worker line cannot bloat the queue snapshot.
    """
    reason = None
    for line in (log_buffer or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("repair-unscaffolded:"):
            reason = stripped.partition(":")[2].strip()[:300]
    return reason or None


def _repair_worker_thread(issue_number: int,
                          repo: str = "remote-server",
                          focus: str = "") -> None:
    """Run the repo-scoped repair worker and record state and logs.

    Single-flight: dispatch claims the slot before spawning, and the
    same values are re-written here (idempotent); schedules rely on
    _claim_due_schedule. One deadline (REPAIR_WORKER_TIMEOUT) bounds
    both blocking points — the stdout drain and the wait — so a
    stuck run is killed and records failed. Never raises out.
    """
    global _repair_proc, _repair_abort
    with repair_state_lock:
        repair_state["status"] = "running"
        repair_state["timestamp"] = time.time()
        repair_state["issue"] = issue_number
        repair_state["repo"] = repo
        repair_state["log_buffer"] = (
            f"Dispatched repair worker for issue #{issue_number} "
            f"({repo})"
            + (" [group focus]\n" if focus else "\n"))
        repair_state["exit_code"] = None
        repair_state["failure_reason"] = None
        _repair_proc = None
        _repair_abort = False
    run = _repair_runner or subprocess
    cmd = ["python3", "-m", "backend.repair_worker",
           "--issue", str(issue_number), "--repo", repo]
    if focus:
        cmd += ["--body", focus]
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
        started = time.monotonic()
        timed_out = False

        def _fire_timeout():
            # Watchdog for a stdout-drain stuck on a silent worker:
            # killing the proc drives the pipe to EOF so the drain
            # ends and wait() returns. Escalates terminate -> kill
            # like repair_abort (a SIGTERM-ignoring spewer must not
            # hold the drain past the deadline).
            nonlocal timed_out
            with repair_state_lock:
                if repair_state["status"] != "running":
                    return
                timed_out = True
            try:
                proc.terminate()
                proc.wait(5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        timer = threading.Timer(REPAIR_WORKER_TIMEOUT, _fire_timeout)
        timer.daemon = True
        timer.start()
        try:
            if proc.stdout:
                for line in proc.stdout:
                    with repair_state_lock:
                        repair_state["log_buffer"] += line
                        if len(repair_state["log_buffer"]) > MAX_OUTPUT:
                            repair_state["log_buffer"] = \
                                repair_state["log_buffer"][-MAX_OUTPUT:]
            budget = REPAIR_WORKER_TIMEOUT - (time.monotonic() - started)
            try:
                code = proc.wait(timeout=max(1, budget))
            except subprocess.TimeoutExpired:
                # Wait-blocked past the deadline (the drain already
                # ended): the watchdog may still be in its grace
                # sleep, so kill here and reap.
                timed_out = True
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    code = proc.wait()
                except Exception:
                    code = -9
        finally:
            timer.cancel()
        with repair_state_lock:
            _repair_proc = None
            if _repair_abort and code is not None and code < 0:
                # Abort only counts when it killed the proc (signal
                # exit); a natural exit after a late abort stays done.
                repair_state["status"] = "aborted"
                repair_state["exit_code"] = None
                repair_state["log_buffer"] += "\nAborted by user.\n"
            elif timed_out and code != 0:
                # Timeout only counts when it killed the proc; a
                # natural exit at the deadline stays done, same rule
                # as the abort above.
                repair_state["status"] = "failed"
                repair_state["exit_code"] = code
                repair_state["failure_reason"] = _extract_failure_reason(
                    repair_state["log_buffer"])
                repair_state["log_buffer"] += (
                    "\nRepair worker timed out after "
                    f"{REPAIR_WORKER_TIMEOUT}s and was killed.\n")
            else:
                repair_state["status"] = "done" if code == 0 else "failed"
                repair_state["exit_code"] = code
                repair_state["failure_reason"] = _extract_failure_reason(
                    repair_state["log_buffer"]) \
                    if code != 0 else None
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


def _repair_verify_preview(issue_number: int,
                           repo: str = "remote-server") -> dict:
    """Parsed verification command + allowlist verdict for an issue.

    Shared by the preview endpoint (the confirmer sees the command)
    and dispatch (refuse to spawn rejected commands). Scoped to the
    given repo; schedules pass the default (stack repo).
    """
    try:
        gh = repo_gh(repo)
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


def trigger_repair(issue_number: int, repo: str = "remote-server",
                   focus: str = ""):
    t = threading.Thread(target=_repair_worker_thread,
                         args=(issue_number, repo, focus), daemon=True)
    t.start()
    return t


def _ensure_repair_idle() -> None:
    """Single-flight guard: 409 when a worker holds the slot."""
    with repair_state_lock:
        if repair_state["status"] == "running":
            raise HTTPException(status_code=409,
                                detail="repair worker busy")


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
        # rather than running a second ticker. Repair-owned loop.
        # Read off the package (not the defining submodule): the import
        # re-runs every tick so schedule tests can patch
        # backend.notify.fire_due_digest and have it land here.
        try:
            from backend import notify as _notify_pkg
            _notify_pkg.fire_due_digest()
        except Exception:
            pass
        # Third job on the shared tick (#652): budget-watch alerts
        # ride this loop like digests — never a second ticker.
        # Same package-attr read so backend.notify.fire_budget_watch
        # patches land here too.
        try:
            from backend import notify as _notify_pkg2
            _notify_pkg2.fire_budget_watch()
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


# Cross-repo open-issue discovery (issue #882): repo=all fans out
# like the #610 inbox rollup. ALL_ISSUES mirrors inbox.ALL_REPOS
# without importing the inbox router (no cycle).
ALL_ISSUES = "all"


def list_issue_repo_names() -> list:
    """Injectable seam: known checkout names in picker order."""
    from backend.core.git_helpers import list_repos

    return [r.get("name", "") for r in list_repos()
            if isinstance(r, dict) and r.get("name")]


def list_open_issues(repo: str) -> list:
    """Open issue rows for one repo: the gh read + body truncation
    the per-repo endpoint has always served. Raises ValueError for
    an unknown repo (repo_gh) and anything else on gh failure."""
    gh = repo_gh(repo)
    rows = run_gh_json(gh, ["issue", "list", "--repo", gh,
                            "--state", "open", "--limit", "50",
                            "--json", ISSUE_LIST_FIELDS], timeout=60)
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get("body"), str) \
                and len(row["body"]) > 600:
            row["body"] = row["body"][:600] + "…"
    return rows


def rollup_issues() -> dict:
    """All-repos open-issue discovery: fan out per repo, tagging
    each row with its repo. A repo whose gh read fails is skipped
    (discovery degrades; the per-repo path still 500s loudly)."""
    tagged = []
    for name in list_issue_repo_names():
        try:
            rows = list_open_issues(name)
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict):
                tagged.append({**row, "repo": name})
    return {"repo": ALL_ISSUES, "issues": tagged}


@router.get("/issues", response_model=IssueList)
def issues(repo: str = "remote-server") -> dict:
    if repo == ALL_ISSUES:
        return rollup_issues()
    # Validate first: an unknown repo is a 400, while any gh/read
    # failure below (including JSON decode, a ValueError) is a 500 —
    # the same split the inline read has always made.
    try:
        repo_gh(repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        rows = list_open_issues(repo)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:500])
    return {"repo": repo, "issues": rows}


def _repo_label_names(gh: str) -> list:
    """Sorted repo label names via gh (typeahead source)."""
    rows = run_gh_json(gh, ["label", "list", "--repo", gh,
                            "--limit", "100", "--json", "name"],
                       timeout=60)
    return sorted({r.get("name", "") for r in rows
                   if isinstance(r, dict)
                   and isinstance(r.get("name"), str)
                   and r.get("name", "").strip()})


def _validate_create_labels(labels, gh: str) -> list:
    """Canonical label names for create-issue, or HTTP 400.

    Each entry must be a well-formed name (same shape rule as
    /api/issue-label) and must exist on the repo — the repo's own
    label set is the allowlist, so unknown names fail here with a
    400 instead of failing inside gh as a 500. Matching is
    case-insensitive; the repo's canonical spelling is returned,
    deduplicated, order-preserving.
    """
    if not labels:
        return []
    wanted = []
    for item in labels:
        name = parse_label_name(item)
        if name is None:
            raise HTTPException(status_code=400,
                                detail="invalid label name")
        wanted.append(name)
    try:
        repo_names = _repo_label_names(gh)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:500])
    canonical = {n.lower(): n for n in repo_names}
    out = []
    for name in wanted:
        hit = canonical.get(name.lower())
        if hit is None:
            raise HTTPException(status_code=400,
                                detail=f"unknown label: {name[:100]}")
        if hit not in out:
            out.append(hit)
    return out


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
    labels = _validate_create_labels(body.labels, gh)
    argv = ["issue", "create", "--repo", gh,
           "--title", body.title.strip(),
           "--body", body.body.strip()]
    for name in labels:
        argv += ["--label", name]
    try:
        out = run_gh(gh, argv, timeout=120)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:500])
    lines = out.strip().splitlines()
    return {"ok": True, "url": (lines[-1] if lines else "")[:500]}


# Scaffold sections required by the lane gate (td gh validate-issue,
# `td gh scaffold-issue` skeleton). issue_draft must emit every one so
# the generated body validates clean as generated (#437).
DRAFT_SCAFFOLD_SECTIONS = (
    "Problem Statement",
    "Goal",
    "Non-Goals",
    "Proposed Approach",
    "Alternatives Considered",
    "Architectural Impact",
    "Scope",
    "UNDERSTAND THE ISSUE",
    "DETERMINE APPROACH",
    "SPECIFY SCOPE",
    "DEFINITION OF DONE",
)

DRAFT_DONE_CHECKLIST = (
    "- [ ] Investigate root cause\n"
    "- [ ] Apply automated or manual fix\n"
    "- [ ] Verify test suite passes\n"
)


@router.post("/issue-draft", response_model=IssueDraft)
def issue_draft(body: IssueDraftRequest) -> dict:
    try:
        repo_gh(body.repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    cause = body.cause.strip()
    details = body.details.strip()
    problem = (f"Reported from {body.source} findings on {body.repo}."
               + (f"\n\n{cause}" if cause else ""))
    understand = (cause if cause
                  else "[Provide content for UNDERSTAND THE ISSUE]")
    scope = (f"```\n{details}\n```" if details
             else "[Provide content for Scope]")
    sections = {
        "Problem Statement": problem,
        "Goal": "[Provide content for Goal]",
        "Non-Goals": "[Provide content for Non-Goals]",
        "Proposed Approach": "[Provide content for Proposed Approach]",
        "Alternatives Considered":
            "[Provide content for Alternatives Considered]",
        "Architectural Impact": "[Provide content for Architectural Impact]",
        "Scope": scope,
        "UNDERSTAND THE ISSUE": understand,
        "DETERMINE APPROACH": "[Provide content for DETERMINE APPROACH]",
        "SPECIFY SCOPE": "[Provide content for SPECIFY SCOPE]",
        "DEFINITION OF DONE": DRAFT_DONE_CHECKLIST.rstrip("\n"),
    }
    md = "".join(f"# {name}\n\n{sections[name]}\n\n"
                 for name in DRAFT_SCAFFOLD_SECTIONS)
    return {"ok": True, "title": body.title, "body": md}


@router.get("/labels", response_model=LabelList)
def repo_labels(repo: str = "remote-server") -> dict:
    try:
        gh = repo_gh(repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        names = _repo_label_names(gh)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:500])
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
def repair_verify_command(issue: str = "",
                          repo: str = "remote-server") -> dict:
    issue_number = parse_issue_number(issue)
    if issue_number is None:
        raise HTTPException(
            status_code=400, detail="invalid issue number or url")
    return _repair_verify_preview(issue_number, repo)


@router.post("/repair/dispatch", response_model=RepairDispatchResult)
def repair_dispatch(body: RepairDispatchRequest) -> dict:
    global _repair_proc, _repair_abort
    from backend.core.validation import require_confirm

    require_confirm(body, "repair-dispatch")
    issue_number = parse_issue_number(body.issue)
    if issue_number is None:
        raise HTTPException(
            status_code=400, detail="invalid issue number or url")
    focus = body.focus if isinstance(body.focus, str) else ""
    if len(focus) > REPAIR_FOCUS_MAX:
        raise HTTPException(status_code=400, detail="focus too large")
    try:
        gh = repo_gh(body.repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    repo_name = gh.partition("/")[2]
    from backend.core.git_helpers import validate_action_branch

    try:
        validate_action_branch(repo_name, body.branch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _ensure_repair_idle()
    preview = _repair_verify_preview(issue_number, repo_name)
    if not preview["allowlisted"]:
        raise HTTPException(
            status_code=400,
            detail=preview["error"] or "verification command rejected")
    with repair_state_lock:
        # Atomic claim: a second dispatch landing between the idle
        # check above and the worker thread's own running-write
        # still 409s, so back-to-back double dispatch is safe. The
        # thread re-writes the same values on start (idempotent).
        if repair_state["status"] == "running":
            raise HTTPException(status_code=409,
                                detail="repair worker busy")
        repair_state["status"] = "running"
        repair_state["timestamp"] = time.time()
        repair_state["issue"] = issue_number
        repair_state["repo"] = repo_name
        repair_state["log_buffer"] = (
            f"Dispatched repair worker for issue #{issue_number} "
            f"({repo_name})"
            + (" [group focus]\n" if focus else "\n"))
        repair_state["exit_code"] = None
        repair_state["failure_reason"] = None
        _repair_proc = None
        _repair_abort = False
    # Focus rides positionally only when set: the bare two-arg call
    # is the long-standing whole-issue shape.
    if focus:
        trigger_repair(issue_number, repo_name, focus)
    else:
        trigger_repair(issue_number, repo_name)
    return {"ok": True, "issue": issue_number, "status": "running",
            "repo": repo_name}


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
