"""jobs router: job registry + allowlisted execution endpoints.

Ports of the buttons/server.py handlers:
- GET  /api/jobs        (job list, newest first)
- GET  /api/jobs/stream (SSE job-list push; polling fallback stays)
- POST /api/jobs/clear  (clear terminal jobs, or all with {"all": true}; 409 while a run is active)
- POST /api/jobs/run     (start a run for an allowlisted action)
- POST /api/jobs/run-td  (start a run for one validated td command)
- GET  /api/jobs/runs/{rid} (run progress snapshot)
- GET  /api/jobs/runs/compare (unified diff of two runs' outputs)
- GET  /api/jobs/runs/{rid}/stream (SSE per-run push; ends on done)
- POST /api/jobs/runs/{rid}/cancel (cancel a running run: 202/404/409)
- POST /api/jobs/run-plan (start a run for one allowlisted td planning guide)
- POST /api/jobs/run-ux-audit (start a dispatched site UX audit)
- POST /api/jobs/run-review (start a local-model PR review)
- GET  /api/jobs/ux-audit/groups (finding groups from the latest audit)
- POST /api/jobs/schedule (schedule a non-destructive action/run-td)
- GET  /api/jobs/schedule (list action schedules + next runs)
- DELETE /api/jobs/schedule/{id} (delete one action schedule)

Execution goes through backend.exec.engine, which enforces the
same allowlists, timeouts, and output truncation as the original.
"""

import difflib
import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.api.routers.chat import SSE_HEADERS, sse_frame
from backend.exec.engine import (
    ALLOWED_ACTIONS,
    DESTRUCTIVE_ACTIONS,
    REPO_SCOPED_ACTIONS,
    RunRegistry,
    build_plan_argv,
    execute_run,
    runs_store_path,
    validate_td_command,
    validate_ux_audit_argv,
)

RID_RE = re.compile(r"^[0-9a-f]{12}$")
RUN_TAIL = 6000

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

# Durable run history (#461): the snapshot path rehydrates on boot,
# so the Jobs view survives console restarts.
registry = RunRegistry(store_path=runs_store_path())


class JobRecord(BaseModel):
    id: str
    kind: str = ""
    name: str = ""
    repo: str = ""
    done: bool = False
    exit_code: int | None = None
    at: float | None = None
    # Replay params for one-click re-run: recorded td argv for
    # run-td/run-plan rows, [] for plain-action rows (action+repo
    # replay).
    td_argv: list[str] = Field(default_factory=list)


class JobList(BaseModel):
    jobs: list[JobRecord] = Field(default_factory=list)


class JobsHealth(BaseModel):
    ok: bool = True


class ClearRequest(BaseModel):
    all: bool = False
    intent: str = ""
    confirmed: bool = False


class ClearResult(BaseModel):
    ok: bool = True
    cleared: int = 0


class RunRequest(BaseModel):
    action: str = ""
    repo: str = "remote-server"
    intent: str = ""
    confirmed: bool = False
    # Explicit branch for the run (#708): validated against the
    # checkout when present; blank behaves exactly as today.
    branch: str = ""


class RunStarted(BaseModel):
    id: str
    # Queue state (issue #1025): capacity never 409s — beyond
    # MAX_CONCURRENT_RUNS the run waits queued and the response
    # names its place in line.
    queued: bool = False
    position: int = 0


class RunTdRequest(BaseModel):
    command: str = ""
    repo: str = "remote-server"
    # Explicit branch for the run (#708): validated against the
    # checkout when present; blank behaves exactly as today.
    branch: str = ""


class RunPlanRequest(BaseModel):
    plan: str = ""
    repo: str = "remote-server"
    pr: str = ""
    prs: list[str] = Field(default_factory=list)
    target: str = ""


@router.get("/health", response_model=JobsHealth)
def health() -> dict:
    return {"ok": True}


def _job_rows_locked() -> list:
    """Job-list rows, newest first. Caller must hold the registry
    lock: the push generators snapshot inside the condition wait,
    which a locking helper could not do (threading.Lock is not
    reentrant, and snapshot-then-read would race)."""
    ordered = sorted(registry.runs.values(),
                     key=lambda r: r.get("seq", 0), reverse=True)
    return [{"id": r["id"], "kind": r.get("action", ""),
             "name": r.get("action", ""), "repo": r.get("repo", ""),
             "done": r["done"], "exit_code": r.get("exit_code"),
             "at": r.get("started_at"),
             "td_argv": r.get("td_argv", [])}
            for r in ordered]


def _job_rows() -> list:
    """Locking snapshot. Shared by the polling endpoint and the
    push stream so both always agree."""
    with registry._lock:
        return _job_rows_locked()


@router.get("", response_model=JobList)
def list_jobs() -> dict:
    return {"jobs": _job_rows()}


# Idle keepalive between snapshots: proxies drop quiet streams, and
# EventSource treats any bytes as liveness.
JOBS_STREAM_HEARTBEAT = 15.0

_MISSING = object()


def _jobs_events(heartbeat: float = JOBS_STREAM_HEARTBEAT):
    """Infinite SSE: full job-list snapshot whenever the rows
    change. First frame is immediate (last starts unmatched), so
    clients never wait for the first paint. Output appends bump
    the registry but leave rows identical, so the list stream
    stays quiet while a run chats — status changes only."""
    last = _MISSING
    while True:
        # Yields stay outside the lock: a suspended generator must
        # never hold it (slow clients would stall run workers).
        # Missed notifies are impossible anyway — the predicate is
        # state-based, and wait_for evaluates it before waiting.
        with registry._cond:
            registry._cond.wait_for(
                lambda: _job_rows_locked() != last, timeout=heartbeat)
            rows = _job_rows_locked()
            changed = rows != last
            last = rows
        if not changed:
            yield ":heartbeat\n\n"
            continue
        yield sse_frame("jobs", {"jobs": rows})


@router.get("/stream")
def jobs_stream() -> StreamingResponse:
    return StreamingResponse(_jobs_events(),
                             media_type="text/event-stream",
                             headers=SSE_HEADERS)


@router.post("/clear", response_model=ClearResult)
def clear_jobs(body: ClearRequest) -> dict:
    from backend.core import require_confirm

    require_confirm(body, "jobs-clear")
    count, err = registry.clear_jobs(all_jobs=bool(body.all))
    if err is not None:
        raise HTTPException(status_code=409, detail=err)
    return {"ok": True, "cleared": count}


def _repo_target(repo: str) -> tuple[str, str]:
    """Validate a repo name; return (gh, td_cwd). Raises ValueError."""
    from backend.core import get_repo_status, validate_repo_name

    name = validate_repo_name(repo)
    try:
        get_repo_status(name)
    except (ValueError, FileNotFoundError, RuntimeError):
        raise ValueError("unknown repo")
    gh = f"arii/{name}"
    td_cwd = "/srv/remote-server" if name == "remote-server" \
        else f"/srv/repos/{name}"
    return gh, td_cwd


def _launch(run_id: str, action: str, repo: str,
            td_argv=None) -> None:
    (gh, td_cwd) = _repo_target(repo) \
        if action in REPO_SCOPED_ACTIONS else ("", "")
    worker = threading.Thread(
        target=execute_run,
        args=(registry, run_id, action, repo, gh, td_cwd),
        kwargs={"td_argv": td_argv},
        daemon=True)
    worker.start()


def _start_dispatch(action: str, repo: str, td_argv=None) -> dict:
    """start_run + worker launch with a queue-aware response.

    Capacity never 409s (issue #1025): beyond
    MAX_CONCURRENT_RUNS the run waits queued and the worker parks
    in acquire() until promotion, so the response names its place
    in line instead of failing. Only validation errors raise.
    """
    run_id, err = registry.start_run(action, repo, td_argv=td_argv)
    if err:
        raise HTTPException(
            status_code=409 if "already in progress" in err else 400,
            detail=err,
        )
    try:
        _launch(run_id, action, repo, td_argv=td_argv)
    except Exception as exc:
        # _launch runs after start_run took a slot; release it so a
        # failed launch can't wedge the engine (#294). Record the
        # error on the RUN record too (#1152, mirror of #1149 for
        # the scheduled path): otherwise a launch failure — e.g.
        # _repo_target rejecting a corrupt checkout — finishes exit
        # 1 with EMPTY output.
        registry.append(run_id, f"error: {exc}\n")
        registry.finish_run(run_id, 1)
        raise
    return {"id": run_id,
            "queued": bool(registry.runs.get(run_id, {}).get("queued")),
            "position": registry.queue_position(run_id)}


@router.post("/run", response_model=RunStarted)
def start_run(body: RunRequest) -> dict:
    if body.action not in ALLOWED_ACTIONS:
        raise HTTPException(status_code=400, detail="unknown action")
    if body.action in DESTRUCTIVE_ACTIONS:
        from backend.core import require_confirm

        require_confirm(body, "jobs-run")
    if body.action in REPO_SCOPED_ACTIONS:
        try:
            _repo_target(body.repo)
        except ValueError:
            raise HTTPException(status_code=400, detail="unknown repo")
    from backend.core import validate_action_branch

    try:
        validate_action_branch(body.repo, body.branch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _start_dispatch(body.action, body.repo)


@router.post("/run-td", response_model=RunStarted)
def start_run_td(body: RunTdRequest) -> dict:
    # Preflight runs here for editors/agents only
    # ({"command": "td --json preflight"}); no SPA view references it.
    tokens = validate_td_command(body.command)
    if not tokens:
        raise HTTPException(status_code=400, detail="bad repo or command")
    try:
        _repo_target(body.repo)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad repo or command")
    from backend.core import validate_action_branch

    try:
        validate_action_branch(body.repo, body.branch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _start_dispatch("run-td", body.repo, td_argv=tokens)


class RunProgress(BaseModel):
    done: bool = False
    output: str = ""
    exit_code: int | None = None
    action: str = ""
    repo: str = ""
    td_argv: list[str] = Field(default_factory=list)


def _run_progress_locked(rid: str) -> dict | None:
    """Run progress payload, or None when the run is gone. Caller
    must hold the registry lock (see _job_rows_locked)."""
    run = registry.runs.get(rid)
    if run is None:
        return None
    out = run.get("output", "")
    return {"done": run["done"], "output": out[-RUN_TAIL:],
            "exit_code": run.get("exit_code"),
            "action": run.get("action", ""),
            "repo": run.get("repo", ""),
            "td_argv": run.get("td_argv", [])}


# Compare cap: ~1MB total input (500K per side, tail-kept). The
# engine already caps stored outputs at 200K, so this is purely
# defensive — giant logs must never OOM the diff.
COMPARE_MAX_PER_SIDE = 500_000


class RunCompare(BaseModel):
    a_id: str = ""
    b_id: str = ""
    unified_diff: str = ""
    truncated: bool = False


# Declared before /runs/{rid}: Starlette matches in order, and
# "compare" must not fall into the rid slot (RID_RE would 404 it).
@router.get("/runs/compare", response_model=RunCompare)
def compare_runs(a: str = "", b: str = "") -> dict:
    """Unified diff of two runs' FULL outputs (#657).

    Same RID_RE + 404 discipline as run_progress: bad-shape and
    missing/cleared ids are both "unknown run".
    """
    if not RID_RE.match(a) or not RID_RE.match(b):
        raise HTTPException(status_code=404, detail="unknown run")
    with registry._lock:
        run_a = registry.runs.get(a)
        run_b = registry.runs.get(b)
        if run_a is None or run_b is None:
            raise HTTPException(status_code=404, detail="unknown run")
        out_a = run_a.get("output", "")
        out_b = run_b.get("output", "")
    truncated = False
    if len(out_a) > COMPARE_MAX_PER_SIDE:
        out_a = out_a[-COMPARE_MAX_PER_SIDE:]
        truncated = True
    if len(out_b) > COMPARE_MAX_PER_SIDE:
        out_b = out_b[-COMPARE_MAX_PER_SIDE:]
        truncated = True
    diff = "\n".join(difflib.unified_diff(
        out_a.splitlines(), out_b.splitlines(),
        fromfile=f"a/{a}", tofile=f"b/{b}", lineterm=""))
    return {"a_id": a, "b_id": b, "unified_diff": diff,
            "truncated": truncated}


@router.get("/runs/{rid}", response_model=RunProgress)
def run_progress(rid: str) -> dict:
    if not RID_RE.match(rid):
        raise HTTPException(status_code=404, detail="unknown run")
    with registry._lock:
        payload = _run_progress_locked(rid)
    if payload is None:
        raise HTTPException(status_code=404, detail="unknown run")
    return payload


class CancelRunResult(BaseModel):
    ok: bool = True
    id: str = ""


@router.post("/runs/{rid}/cancel", status_code=202,
             response_model=CancelRunResult)
def cancel_run(rid: str) -> dict:
    """Cancel a running run (#589): flag + SIGTERM the step tree.

    202 once the run is flagged (the worker finishes it with the
    note + exit 130), 404 for a missing record, 409 when the run
    already finished. No confirm: cancel stops spend.
    """
    if not RID_RE.match(rid):
        raise HTTPException(status_code=404, detail="unknown run")
    rid_out, err = registry.cancel_run(rid)
    if err == "unknown run":
        raise HTTPException(status_code=404, detail="unknown run")
    if err is not None:
        raise HTTPException(status_code=409, detail=err)
    return {"ok": True, "id": rid_out}


def _run_events(rid: str, heartbeat: float = JOBS_STREAM_HEARTBEAT):
    """SSE progress for one run: a frame per output/done change,
    then the stream ends after the done frame. A cleared run ends
    with an error frame instead, which tells the client to fall
    back to a snapshot fetch."""
    last = _MISSING
    while True:
        with registry._cond:
            registry._cond.wait_for(
                lambda: _run_progress_locked(rid) != last,
                timeout=heartbeat)
            payload = _run_progress_locked(rid)
            changed = payload != last
            last = payload
        if payload is None:
            # Named `gone`, not `error`: an SSE frame named error
            # would also trip the client's onerror handler.
            yield sse_frame("gone", {"error": "unknown run"})
            return
        if not changed:
            yield ":heartbeat\n\n"
            continue
        yield sse_frame("progress", payload)
        if payload["done"]:
            return


@router.get("/runs/{rid}/stream")
def run_stream(rid: str) -> StreamingResponse:
    if not RID_RE.match(rid):
        raise HTTPException(status_code=404, detail="unknown run")
    with registry._lock:
        if registry.runs.get(rid) is None:
            raise HTTPException(status_code=404, detail="unknown run")
    return StreamingResponse(_run_events(rid),
                             media_type="text/event-stream",
                             headers=SSE_HEADERS)


def _ux_audit_module():
    """Lazy deep-agent ux_audit import (stdlib-only at import, so
    this works without the venv; same seam style as the review
    module's s5tools import). Raises ImportError when the
    deep-agent tree is missing."""
    from backend.exec.engine import deep_agent_dir

    deep_agent = deep_agent_dir()
    if deep_agent not in sys.path:
        sys.path.insert(0, deep_agent)
    import ux_audit

    return ux_audit


class RunUxAuditRequest(BaseModel):
    site: str = ""
    max_pages: int = 25
    branch: str = ""


class UxAuditGroup(BaseModel):
    index: int = 0
    category: str = ""
    title: str = ""
    count: int = 0
    worst: str = ""
    body: str = ""


class UxAuditGroups(BaseModel):
    site: str = ""
    groups: list = Field(default_factory=list)


@router.post("/run-ux-audit", response_model=RunStarted)
def start_run_ux_audit(body: RunUxAuditRequest) -> dict:
    """Dispatch a multi-page UX audit (#850): crawl + deterministic
    impeccable pass + file ONE findings issue to the site's repo.

    First-class run (single-flighted, listed, ledgered) like
    run-plan: clients poll /runs/{rid}, whose tail ends in
    AUDIT_DONE <repo>#<num> <url> on success. Deterministic (no
    --model); audit-only, never changes site content.
    """
    try:
        ux_audit = _ux_audit_module()
    except ImportError as exc:
        raise HTTPException(status_code=500,
                            detail=f"ux audit unavailable: {exc}")
    try:
        ux_audit.site_config(body.site)
    except ValueError:
        raise HTTPException(status_code=400, detail="unknown site")
    pages = body.max_pages
    if isinstance(pages, bool) or not isinstance(pages, int) \
            or not 1 <= pages <= 200:
        raise HTTPException(status_code=400, detail="bad max_pages")
    argv = ["ux-audit", body.site, "--max-pages", str(pages)]
    return _start_dispatch("ux-audit", "remote-server", td_argv=argv)


@router.get("/ux-audit/groups", response_model=UxAuditGroups)
def ux_audit_groups(site: str = "") -> dict:
    """Finding groups from a site's latest audit report — the
    one-tap assign payloads (each body is a task spec for one
    repair dispatch with focus). 404 until the first run lands."""
    try:
        ux_audit = _ux_audit_module()
    except ImportError as exc:
        raise HTTPException(status_code=500,
                            detail=f"ux audit unavailable: {exc}")
    try:
        groups = ux_audit.report_groups(site)
    except ValueError:
        raise HTTPException(status_code=400, detail="unknown site")
    if groups is None:
        raise HTTPException(status_code=404, detail="no report yet")
    return {"site": site, "groups": groups}


class RunReviewRequest(BaseModel):
    repo: str = "remote-server"
    pr: int = 0
    post: bool = False


@router.post("/run-review", response_model=RunStarted)
def start_run_review(body: RunReviewRequest) -> dict:
    """Dispatch a local-model PR review (#886): review plan +
    Ollama/Nebius diff review at zero marginal cost.

    First-class run (single-flighted, listed, ledgered) like
    run-ux-audit: clients poll /runs/{rid} for the markdown body
    (or the plan JSON on a dry run). Read-only by default: the
    run dry-runs (prints the plan, no model call, no post) unless
    post=true explicitly asks for a posted review.
    """
    try:
        _repo_target(body.repo)
    except ValueError:
        raise HTTPException(status_code=400, detail="unknown repo")
    pr = body.pr
    if isinstance(pr, bool) or not isinstance(pr, int) or pr <= 0:
        raise HTTPException(status_code=400, detail="bad pr")
    argv = ["review-pr", str(pr)]
    if body.post:
        argv.append("--post")
    return _start_dispatch("review-pr", body.repo, td_argv=argv)


@router.post("/run-plan", response_model=RunStarted)
def start_run_plan(body: RunPlanRequest) -> dict:
    argv, err = build_plan_argv(body.plan, body.pr, body.prs, body.target)
    if err:
        raise HTTPException(status_code=400, detail=err)
    # First-class run (#452): single-flighted, listed in /api/jobs,
    # and ledgered through the standard run path — no inline 5-minute
    # await. The plan runs in the stack dir regardless of repo (as the
    # old inline handler did); clients poll /runs/{rid} for the output
    # JSON, which carries plan_path.
    return _start_dispatch("run-plan", body.repo, td_argv=argv)


PLAN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.md$")
PLAN_READ_MAX = 20000


class PlanList(BaseModel):
    repo: str = ""
    plans: list = Field(default_factory=list)


class PlanRead(BaseModel):
    repo: str = ""
    name: str = ""
    content: str = ""


def _plans_dir(repo: str) -> Path:
    """Validated .agents/plans dir for a repo. Raises ValueError."""
    from backend.core import get_repo_status, resolve_repo_path, \
        validate_repo_name
    from backend.exec.engine import stack_dir

    try:
        name = validate_repo_name(repo)
        get_repo_status(name)
    except (ValueError, FileNotFoundError, RuntimeError):
        raise ValueError("unknown repo")
    base = Path(stack_dir()) if name == "remote-server" \
        else resolve_repo_path(name)
    return base / ".agents" / "plans"


@router.get("/plans", response_model=PlanList)
def list_plans(repo: str = "remote-server") -> dict:
    try:
        plans_dir = _plans_dir(repo)
    except ValueError:
        raise HTTPException(status_code=400, detail="unknown repo")
    rows = []
    if plans_dir.is_dir():
        for child in sorted(plans_dir.iterdir(), reverse=True):
            if child.is_file() and PLAN_NAME_RE.match(child.name):
                try:
                    st = child.stat()
                except OSError:
                    continue
                rows.append({"name": child.name, "size": st.st_size,
                             "mtime": st.st_mtime})
    return {"repo": repo, "plans": rows[:50]}


@router.get("/plans/{name}", response_model=PlanRead)
def read_plan(name: str, repo: str = "remote-server") -> dict:
    if not PLAN_NAME_RE.match(name):
        raise HTTPException(status_code=404, detail="unknown plan")
    try:
        plans_dir = _plans_dir(repo)
    except ValueError:
        raise HTTPException(status_code=400, detail="unknown repo")
    path = plans_dir / name
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise HTTPException(status_code=404, detail="unknown plan")
    return {"repo": repo, "name": name, "content": content[:PLAN_READ_MAX]}


# Scheduled jobs actions (#655). Any non-destructive action (plus
# run-td argv) can carry a cadence; due runs fire through the same
# registry internals as manual launches, so single-flight applies
# and scheduled/manual runs are indistinguishable downstream. The
# store is a JSON file beside the digest schedule so cadences
# survive restarts; firing rides the shared tick in
# backend/api/routers/issues.py (single ticker — never add a second
# one here). Destructive actions are a hard exclusion: confirm
# gates need a live operator. quarter-hourly exists for the notify
# check loop (#822): 15 min balances push timeliness vs gh load.
ACTION_SCHEDULE_INTERVALS = {"daily": 24 * 3600, "weekly": 7 * 24 * 3600,
                             "quarter-hourly": 15 * 60}

DEFAULT_ACTION_SCHEDULE_PATH = "/var/lib/buttons/jobs_schedules.json"

action_schedule_lock = threading.Lock()


def action_schedule_path() -> str:
    """Current action-schedule file path (env override read
    dynamically so tests can point it at tmp)."""
    return os.environ.get("BUTTONS_JOBS_SCHEDULE_PATH",
                          DEFAULT_ACTION_SCHEDULE_PATH)


def load_action_schedules() -> dict:
    try:
        with open(action_schedule_path(), encoding="utf-8") as f:
            state = json.load(f)
        if isinstance(state, dict):
            return {k: v for k, v in state.items()
                    if isinstance(v, dict)}
    except Exception:
        pass
    return {}


def save_action_schedules(state: dict) -> None:
    path = action_schedule_path()
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


class ActionSchedule(BaseModel):
    id: str = ""
    action: str = ""
    td_argv: list[str] = Field(default_factory=list)
    repo: str = ""
    cadence: str = ""
    next_run: float = 0.0
    last_run: float | None = None
    last_result: dict = Field(default_factory=dict)
    enabled: bool = True


class ActionScheduleRequest(BaseModel):
    action: str = ""
    td_argv: list[str] = Field(default_factory=list)
    repo: str = "remote-server"
    cadence: str = ""
    enabled: bool = True


class ActionScheduleResult(BaseModel):
    ok: bool = True
    schedule: ActionSchedule | None = None


class ActionScheduleList(BaseModel):
    schedules: list[ActionSchedule] = Field(default_factory=list)


class ActionScheduleDeleted(BaseModel):
    ok: bool = True


@router.post("/schedule", response_model=ActionScheduleResult)
def create_action_schedule(body: ActionScheduleRequest) -> dict:
    cadence = (body.cadence or "").strip().lower()
    if cadence not in ACTION_SCHEDULE_INTERVALS:
        valid = ", ".join(sorted(ACTION_SCHEDULE_INTERVALS))
        raise HTTPException(
            status_code=400,
            detail=f"cadence must be {valid} "
                   f"(delete the schedule to stop it)")
    action = (body.action or "").strip()
    if action not in ALLOWED_ACTIONS:
        raise HTTPException(status_code=400, detail="unknown action")
    if action in DESTRUCTIVE_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"action '{action}' is destructive and cannot be "
                   f"scheduled — confirm gates need a live operator; "
                   f"launch it manually from Jobs")
    td_argv = list(body.td_argv or [])
    if td_argv:
        if action == "run-td":
            tokens = validate_td_command(" ".join(td_argv))
            if tokens is None or tokens != td_argv:
                raise HTTPException(
                    status_code=400, detail="bad td command")
        elif action == "ux-audit":
            # Periodic re-audits (#850): the site argv replays
            # through the same step builder as a manual dispatch.
            if not validate_ux_audit_argv(td_argv):
                raise HTTPException(
                    status_code=400, detail="bad ux-audit argv")
        else:
            raise HTTPException(
                status_code=400,
                detail="td_argv is only valid with action "
                       "run-td or ux-audit")
    elif action == "run-td":
        raise HTTPException(
            status_code=400, detail="run-td schedules need td_argv")
    elif action == "ux-audit":
        raise HTTPException(
            status_code=400,
            detail="ux-audit schedules need td_argv")
    repo = (body.repo or "remote-server").strip() or "remote-server"
    if action in REPO_SCOPED_ACTIONS:
        try:
            _repo_target(repo)
        except ValueError:
            raise HTTPException(status_code=400, detail="unknown repo")
    now = time.time()
    sid = uuid.uuid4().hex[:12]
    sched = {"action": action, "td_argv": td_argv, "repo": repo,
             "cadence": cadence,
             "next_run": now + ACTION_SCHEDULE_INTERVALS[cadence],
             "last_run": None, "last_result": {},
             "enabled": bool(body.enabled)}
    with action_schedule_lock:
        state = load_action_schedules()
        state[sid] = sched
        save_action_schedules(state)
    from backend.api.routers.issues import _ensure_schedule_ticker
    _ensure_schedule_ticker()
    return {"ok": True, "schedule": {"id": sid, **sched}}


@router.get("/schedule", response_model=ActionScheduleList)
def list_action_schedules() -> dict:
    with action_schedule_lock:
        state = load_action_schedules()
    return {"schedules": [{"id": sid, **state[sid]}
                          for sid in sorted(state)]}


@router.delete("/schedule/{sid}", response_model=ActionScheduleDeleted)
def delete_action_schedule(sid: str) -> dict:
    with action_schedule_lock:
        state = load_action_schedules()
        if sid not in state:
            raise HTTPException(
                status_code=404, detail="unknown schedule")
        del state[sid]
        save_action_schedules(state)
    return {"ok": True}


def _set_schedule_enabled(sid: str, enabled: bool) -> dict:
    """Flip one schedule's enabled flag, preserving its definition
    and history. next_run is left untouched: a paused schedule
    resumes where it left off (one catch-up fire at most, then the
    original cadence — never a burst)."""
    with action_schedule_lock:
        state = load_action_schedules()
        if sid not in state or not isinstance(state[sid], dict):
            raise HTTPException(
                status_code=404, detail="unknown schedule")
        state[sid]["enabled"] = enabled
        save_action_schedules(state)
        return {"ok": True, "schedule": {"id": sid, **state[sid]}}


@router.post("/schedule/{sid}/pause",
             response_model=ActionScheduleResult)
def pause_action_schedule(sid: str) -> dict:
    return _set_schedule_enabled(sid, False)


@router.post("/schedule/{sid}/resume",
             response_model=ActionScheduleResult)
def resume_action_schedule(sid: str) -> dict:
    return _set_schedule_enabled(sid, True)


def _claim_due_action_schedules(now: float) -> list:
    """Claim every due schedule, advancing each next_run before
    returning so a firing is never double-claimed. Returns
    (id, schedule-snapshot) pairs, or []."""
    claimed = []
    with action_schedule_lock:
        state = load_action_schedules()
        changed = False
        for sid in sorted(state):
            sched = state[sid]
            interval = ACTION_SCHEDULE_INTERVALS.get(
                sched.get("cadence", ""))
            if interval is None:
                continue
            # Paused schedules skip without advancing: next_run,
            # last_run, and last_result stay untouched (#870). Absent
            # flag reads enabled, so pre-pause seeds need no
            # migration.
            if not sched.get("enabled", True):
                continue
            if (sched.get("next_run") or 0) > now:
                continue
            sched["last_run"] = now
            sched["next_run"] = now + interval
            claimed.append((sid, dict(sched)))
            changed = True
        if changed:
            save_action_schedules(state)
    return claimed


def _record_schedule_result(sid: str, result: dict) -> None:
    with action_schedule_lock:
        state = load_action_schedules()
        if sid in state:
            state[sid]["last_result"] = result
            save_action_schedules(state)


def _fire_action_schedule(sid: str, sched: dict, now: float) -> None:
    """Fire one claimed schedule through the standard launch
    internals (registry queue + _launch), recording the run id or
    the error back to the store. Scheduled actions are
    non-destructive by construction, so no confirm gate applies."""
    action = sched.get("action", "")
    repo = sched.get("repo", "remote-server")
    td_argv = sched.get("td_argv") or []
    run_id, err = registry.start_run(
        action, repo, td_argv=list(td_argv) if td_argv else None)
    if err:
        _record_schedule_result(
            sid, {"ok": False, "at": now, "error": str(err)[:300]})
        return
    try:
        _launch(run_id, action, repo, td_argv=list(td_argv) or None)
    except Exception as exc:
        # Same release as start_run: a failed launch must not wedge
        # the engine. Record the error on the RUN record too (#1125:
        # a launch failure — e.g. _repo_target rejecting a repo whose
        # checkout git is corrupt — otherwise leaves exit 1 with
        # EMPTY output, since execute_run never starts and the
        # schedule result alone never reaches the run-detail
        # endpoint).
        registry.append(run_id, f"error: {exc}\n")
        registry.finish_run(run_id, 1)
        _record_schedule_result(
            sid, {"ok": False, "at": now, "error": str(exc)[:300]})
        return
    _record_schedule_result(
        sid, {"ok": True, "at": now, "run_id": run_id})


# Default notify-check seeds (#822): one quarter-hourly
# check-notifications schedule per checkout, so ci_fail/conflict
# pushes and the interval digest fire with zero setup. Seeds
# stagger by two minutes per repo: the engine is single-flight and
# a busy claim still advances (#655 contract), so same-tick seeds
# would starve every repo but the first in sid order forever.
NOTIFY_SEED_ACTION = "check-notifications"
NOTIFY_SEED_CADENCE = "quarter-hourly"
NOTIFY_SEED_STAGGER = 120


def _notify_seed_repos() -> list:
    """Checkout names that can carry a notify seed. Discovery first
    (stack checkout + every repos-dir .git), remote-server always a
    candidate; each must pass _repo_target, so unknown checkouts
    never seed an error-every-15-min schedule."""
    names: list = []
    try:
        from backend.core import list_repos

        for row in list_repos():
            name = row.get("name") if isinstance(row, dict) else None
            if name and name not in names:
                names.append(name)
    except Exception:
        pass
    if "remote-server" not in names:
        names.insert(0, "remote-server")
    out = []
    for name in names:
        try:
            _repo_target(name)
        except ValueError:
            continue
        out.append(name)
    return out


def _notify_seed_path() -> str:
    """Sentinel beside the schedule store: the seeded repo names.
    Separate file (not a store row) so it never renders as a bogus
    schedule in the list endpoint."""
    return action_schedule_path() + ".seeded"


def _load_seed_names(path: str) -> list:
    try:
        with open(path, encoding="utf-8") as f:
            names = json.load(f)
        if isinstance(names, list):
            return [n for n in names if isinstance(n, str)]
    except Exception:
        pass
    return []


def _save_seed_names(path: str, names) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(set(names)), f)
    os.replace(tmp, path)


def _load_notify_seeded() -> list:
    return _load_seed_names(_notify_seed_path())


def _save_notify_seeded(names) -> None:
    _save_seed_names(_notify_seed_path(), names)


def _ensure_default_notify_schedules(now: float | None = None) -> int:
    """Seed one notify schedule per checkout. Returns the seeded
    count. The sentinel records every repo ever seeded: operator
    deletes stick (seeded-but-deleted is never reseeded), while
    brand-new checkouts still get a schedule on a later tick and
    servers that predate the seed get the one-time migration. The
    first seed is due immediately so the first tick fires it; rows
    stagger out by NOTIFY_SEED_STAGGER each. Never raises."""
    try:
        repos = _notify_seed_repos()
        if not repos:
            return 0
        missing = [r for r in repos if r not in _load_notify_seeded()]
        if not missing:
            return 0
        at = time.time() if now is None else now
        with action_schedule_lock:
            done = set(_load_notify_seeded())
            missing = [r for r in repos if r not in done]
            if not missing:
                return 0
            state = load_action_schedules()
            live = {s.get("repo") for s in state.values()
                    if isinstance(s, dict)
                    and s.get("action") == NOTIFY_SEED_ACTION}
            fresh = [r for r in missing if r not in live]
            base = len(live) * NOTIFY_SEED_STAGGER
            for idx, name in enumerate(fresh):
                sid = uuid.uuid4().hex[:12]
                state[sid] = {"action": NOTIFY_SEED_ACTION,
                              "td_argv": [], "repo": name,
                              "cadence": NOTIFY_SEED_CADENCE,
                              "next_run": at + base
                              + idx * NOTIFY_SEED_STAGGER,
                              "last_run": None,
                              "last_result": {},
                              "enabled": True}
            if fresh:
                save_action_schedules(state)
            _save_notify_seeded(done | set(missing))
        return len(fresh)
    except Exception:
        return 0


# Default autofile seeds (#825): one daily autofile-scan schedule
# per checkout, same shape as the notify seeds above. The sentinel
# is a separate file so deleting a notify schedule never re-arms
# (or disarms) the autofile seed for that repo, and vice versa.
# The grid sits half a stagger off the notify grid: daily is an
# exact multiple of quarter-hourly (86400 = 96 * 900), so a shared
# phase would contend single-flight on the same tick every day and
# the loser would slip a full interval; the 60s offset keeps the
# two first-firings (and their steady states) on different ticks.
AUTOFILE_SEED_ACTION = "autofile-scan"
AUTOFILE_SEED_CADENCE = "daily"
AUTOFILE_SEED_OFFSET = 60


def _autofile_seed_path() -> str:
    return action_schedule_path() + ".autofile-seeded"


def _ensure_default_autofile_schedules(
        now: float | None = None) -> int:
    """Seed one autofile schedule per checkout. Same contract as
    the notify seed: operator deletes stick, new checkouts seed on
    a later tick, the first seed is due immediately, rows stagger
    by NOTIFY_SEED_STAGGER each, offset off the notify grid by
    AUTOFILE_SEED_OFFSET. Never raises."""
    try:
        repos = _notify_seed_repos()
        if not repos:
            return 0
        missing = [r for r in repos
                   if r not in _load_seed_names(_autofile_seed_path())]
        if not missing:
            return 0
        at = time.time() if now is None else now
        with action_schedule_lock:
            done = set(_load_seed_names(_autofile_seed_path()))
            missing = [r for r in repos if r not in done]
            if not missing:
                return 0
            state = load_action_schedules()
            live = {s.get("repo") for s in state.values()
                    if isinstance(s, dict)
                    and s.get("action") == AUTOFILE_SEED_ACTION}
            fresh = [r for r in missing if r not in live]
            base = len(live) * NOTIFY_SEED_STAGGER
            for idx, name in enumerate(fresh):
                sid = uuid.uuid4().hex[:12]
                state[sid] = {"action": AUTOFILE_SEED_ACTION,
                              "td_argv": [], "repo": name,
                              "cadence": AUTOFILE_SEED_CADENCE,
                              "next_run": at + base
                              + idx * NOTIFY_SEED_STAGGER
                              + AUTOFILE_SEED_OFFSET,
                              "last_run": None,
                              "last_result": {},
                              "enabled": True}
            if fresh:
                save_action_schedules(state)
            _save_seed_names(_autofile_seed_path(),
                             done | set(missing))
        return len(fresh)
    except Exception:
        return 0


# Default impact seed (#886): one daily console-wide impact-pass
# schedule. A singleton, not per-repo: the pass diffs the console
# itself, so per-repo rows would all do the same work. Same
# contract as the siblings otherwise (operator deletes stick, own
# sentinel file, never raises). The seed still keys off checkout
# discovery: no checkouts, no console to regress, no schedule —
# which also keeps the pre-seed fire tests hermetic (they stub
# discovery to []). The 300s phase keeps the first firing off the
# notify (0s) and autofile (60s) grids: daily is an exact multiple
# of quarter-hourly, so a shared phase would contend single-flight
# on the same tick every day.
IMPACT_SEED_ACTION = "impact-pass"
IMPACT_SEED_CADENCE = "daily"
IMPACT_SEED_OFFSET = 300
IMPACT_SEED_REPO = "remote-server"


def _impact_seed_path() -> str:
    return action_schedule_path() + ".impact-seeded"


def _ensure_default_impact_schedules(
        now: float | None = None) -> int:
    """Seed the one console-wide impact schedule. Returns 1 on the
    first seed, 0 after. Never raises."""
    try:
        if not _notify_seed_repos():
            return 0
        if _load_seed_names(_impact_seed_path()):
            return 0
        at = time.time() if now is None else now
        with action_schedule_lock:
            if _load_seed_names(_impact_seed_path()):
                return 0
            state = load_action_schedules()
            live = any(isinstance(s, dict)
                       and s.get("action") == IMPACT_SEED_ACTION
                       for s in state.values())
            fresh = [] if live else [IMPACT_SEED_REPO]
            for name in fresh:
                sid = uuid.uuid4().hex[:12]
                state[sid] = {"action": IMPACT_SEED_ACTION,
                              "td_argv": [], "repo": name,
                              "cadence": IMPACT_SEED_CADENCE,
                              "next_run": at + IMPACT_SEED_OFFSET,
                              "last_run": None,
                              "last_result": {},
                              "enabled": True}
            if fresh:
                save_action_schedules(state)
            _save_seed_names(_impact_seed_path(), [IMPACT_SEED_ACTION])
        return len(fresh)
    except Exception:
        return 0


# Default supervise seeds (#886): one weekly --supervise schedule
# per checkout, same shape as the autofile seeds above (own
# sentinel, stagger by NOTIFY_SEED_STAGGER, deletes stick, new
# checkouts seed on a later tick). Weekly: the sweep runs a local
# model over the checkout, so it is the heaviest seed. The 540s
# phase keeps the first firings off the notify/autofile/impact
# grids (weekly is a multiple of quarter-hourly too).
SUPERVISE_SEED_ACTION = "supervise"
SUPERVISE_SEED_CADENCE = "weekly"
SUPERVISE_SEED_OFFSET = 540


def _supervise_seed_path() -> str:
    return action_schedule_path() + ".supervise-seeded"


def _ensure_default_supervise_schedules(
        now: float | None = None) -> int:
    """Seed one supervise schedule per checkout. Same contract as
    the autofile seed. Never raises."""
    try:
        repos = _notify_seed_repos()
        if not repos:
            return 0
        missing = [r for r in repos
                   if r not in _load_seed_names(_supervise_seed_path())]
        if not missing:
            return 0
        at = time.time() if now is None else now
        with action_schedule_lock:
            done = set(_load_seed_names(_supervise_seed_path()))
            missing = [r for r in repos if r not in done]
            if not missing:
                return 0
            state = load_action_schedules()
            live = {s.get("repo") for s in state.values()
                    if isinstance(s, dict)
                    and s.get("action") == SUPERVISE_SEED_ACTION}
            fresh = [r for r in missing if r not in live]
            base = len(live) * NOTIFY_SEED_STAGGER
            for idx, name in enumerate(fresh):
                sid = uuid.uuid4().hex[:12]
                state[sid] = {"action": SUPERVISE_SEED_ACTION,
                              "td_argv": [], "repo": name,
                              "cadence": SUPERVISE_SEED_CADENCE,
                              "next_run": at + base
                              + idx * NOTIFY_SEED_STAGGER
                              + SUPERVISE_SEED_OFFSET,
                              "last_run": None,
                              "last_result": {},
                              "enabled": True}
            if fresh:
                save_action_schedules(state)
            _save_seed_names(_supervise_seed_path(),
                             done | set(missing))
        return len(fresh)
    except Exception:
        return 0


def fire_due_action_schedules(now: float | None = None) -> int:
    """Fire every due action schedule. Returns the fired count.
    Never raises: the shared tick must not die on a job failure."""
    try:
        at = time.time() if now is None else now
        _ensure_default_notify_schedules(at)
        _ensure_default_autofile_schedules(at)
        _ensure_default_impact_schedules(at)
        _ensure_default_supervise_schedules(at)
        fired = 0
        for sid, sched in _claim_due_action_schedules(at):
            try:
                _fire_action_schedule(sid, sched, at)
                fired += 1
            except Exception:
                pass
        return fired
    except Exception:
        return 0
