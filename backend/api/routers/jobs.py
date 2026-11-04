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
    run_id, err = registry.start_run(body.action, body.repo)
    if err:
        raise HTTPException(
            status_code=409 if "already in progress" in err else 400,
            detail=err,
        )
    try:
        _launch(run_id, body.action, body.repo)
    except Exception:
        # _launch runs after start_run took the single-flight lock;
        # release it so a failed launch can't wedge the engine (#294).
        registry.finish_run(run_id, 1)
        raise
    return {"id": run_id}


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
    run_id, err = registry.start_run("run-td", body.repo, td_argv=tokens)
    if err:
        raise HTTPException(
            status_code=409 if "already in progress" in err else 400,
            detail=err,
        )
    try:
        _launch(run_id, "run-td", body.repo, td_argv=tokens)
    except Exception:
        registry.finish_run(run_id, 1)
        raise
    return {"id": run_id}


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
    run_id, err = registry.start_run("run-plan", body.repo, td_argv=argv)
    if err:
        raise HTTPException(
            status_code=409 if "already in progress" in err else 400,
            detail=err,
        )
    try:
        _launch(run_id, "run-plan", body.repo, td_argv=argv)
    except Exception:
        registry.finish_run(run_id, 1)
        raise
    return {"id": run_id}


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
# run-td argv) can carry a daily/weekly cadence; due runs fire
# through the same registry internals as manual launches, so
# single-flight applies and scheduled/manual runs are
# indistinguishable downstream. The store is a JSON file beside the
# digest schedule so cadences survive restarts; firing rides the
# shared tick in backend/api/routers/issues.py (single ticker —
# never add a second one here). Destructive actions are a hard
# exclusion: confirm gates need a live operator.
ACTION_SCHEDULE_INTERVALS = {"daily": 24 * 3600, "weekly": 7 * 24 * 3600}

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


class ActionScheduleRequest(BaseModel):
    action: str = ""
    td_argv: list[str] = Field(default_factory=list)
    repo: str = "remote-server"
    cadence: str = ""


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
        raise HTTPException(
            status_code=400,
            detail="cadence must be daily or weekly "
                   "(delete the schedule to stop it)")
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
        if action != "run-td":
            raise HTTPException(
                status_code=400,
                detail="td_argv is only valid with action run-td")
        tokens = validate_td_command(" ".join(td_argv))
        if tokens is None or tokens != td_argv:
            raise HTTPException(
                status_code=400, detail="bad td command")
    elif action == "run-td":
        raise HTTPException(
            status_code=400, detail="run-td schedules need td_argv")
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
             "last_run": None, "last_result": {}}
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
    internals (registry.single-flight + _launch), recording the run
    id or the error back to the store. Scheduled actions are
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
        # the single-flight engine.
        registry.finish_run(run_id, 1)
        _record_schedule_result(
            sid, {"ok": False, "at": now, "error": str(exc)[:300]})
        return
    _record_schedule_result(
        sid, {"ok": True, "at": now, "run_id": run_id})


def fire_due_action_schedules(now: float | None = None) -> int:
    """Fire every due action schedule. Returns the fired count.
    Never raises: the shared tick must not die on a job failure."""
    try:
        at = time.time() if now is None else now
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
