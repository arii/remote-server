"""jobs router: job registry + allowlisted execution endpoints.

Ports of the buttons/server.py handlers:
- GET  /api/jobs        (job list, newest first)
- POST /api/jobs/clear  (clear terminal jobs, or all with {"all": true})
- POST /api/jobs/run     (start a run for an allowlisted action)
- POST /api/jobs/run-td  (start a run for one validated td command)
- POST /api/jobs/run-plan (run one allowlisted td planning guide)

No Jules dispatch surface exists here (cut). Execution goes through
backend.exec.engine, which enforces the same allowlists, timeouts,
and output truncation as the original.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.exec.engine import (
    ALLOWED_ACTIONS,
    RunRegistry,
    build_plan_argv,
    td_run,
    validate_td_command,
)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

registry = RunRegistry()


class JobRecord(BaseModel):
    id: str
    kind: str = ""
    repo: str = ""
    done: bool = False


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


class RunStarted(BaseModel):
    id: str


class RunTdRequest(BaseModel):
    command: str = ""
    repo: str = "remote-server"


class RunPlanRequest(BaseModel):
    plan: str = ""
    repo: str = "remote-server"
    pr: str = ""
    prs: list[str] = Field(default_factory=list)
    target: str = ""


class RunPlanResult(BaseModel):
    ok: bool = True
    output: str = ""
    plan_path: str = ""


@router.get("/health", response_model=JobsHealth)
def health() -> dict:
    return {"ok": True}


@router.get("", response_model=JobList)
def list_jobs() -> dict:
    return {"jobs": registry.list_jobs()}


@router.post("/clear", response_model=ClearResult)
def clear_jobs(body: ClearRequest) -> dict:
    from backend.core import require_confirm

    require_confirm(body, "jobs-clear")
    count = registry.clear_jobs(all_jobs=bool(body.all))
    return {"ok": True, "cleared": count}


@router.post("/run", response_model=RunStarted)
def start_run(body: RunRequest) -> dict:
    if body.action not in ALLOWED_ACTIONS:
        raise HTTPException(status_code=400, detail="unknown action")
    run_id, err = registry.start_run(body.action, body.repo)
    if err:
        raise HTTPException(
            status_code=409 if "already in progress" in err else 400,
            detail=err,
        )
    return {"id": run_id}


@router.post("/run-td", response_model=RunStarted)
def start_run_td(body: RunTdRequest) -> dict:
    # Preflight runs here for editors/agents only
    # ({"command": "td --json preflight"}); no SPA view references it.
    tokens = validate_td_command(body.command)
    if not tokens:
        raise HTTPException(status_code=400, detail="bad repo or command")
    run_id, err = registry.start_run("run-td", body.repo, td_argv=tokens)
    if err:
        raise HTTPException(
            status_code=409 if "already in progress" in err else 400,
            detail=err,
        )
    return {"id": run_id}


@router.post("/run-plan", response_model=RunPlanResult)
def start_run_plan(body: RunPlanRequest) -> dict:
    import json

    argv, err = build_plan_argv(body.plan, body.pr, body.prs, body.target)
    if err:
        raise HTTPException(status_code=400, detail=err)
    import shlex as _shlex

    cmd = " ".join(_shlex.quote(t) for t in argv)
    try:
        out = td_run(cmd, timeout=300)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:500])
    plan_path = ""
    try:
        plan_path = json.loads(out).get("plan_path", "") or ""
    except Exception:
        pass
    return {"ok": True, "output": out[-4000:], "plan_path": plan_path}
