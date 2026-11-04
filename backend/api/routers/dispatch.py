"""dispatch router: cross-repo lane queue (issue #853).

- GET  /api/dispatch/health   liveness
- GET  /api/dispatch/queue    every queued item, oldest first
- GET  /api/dispatch/gates    live per-repo readiness for one item
- POST /api/dispatch/enqueue  queue (repo, issue)
- POST /api/dispatch/start    dispatch one item (confirmed tap)
- POST /api/dispatch/cancel   drop a queued/terminal item

Dispatch executes through the repo-scoped repair worker (same
branch/PR flow as the single-repo console); this router only queues,
gates, and watches. remote-server dispatches exactly as before —
it is repo #1 of N, not a special case.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend import lane_dispatch

router = APIRouter(prefix="/api/dispatch", tags=["dispatch"])

START_INTENT = "dispatch-start"


class DispatchHealth(BaseModel):
    ok: bool = True


class DispatchItem(BaseModel):
    repo: str = ""
    issue: int = 0
    state: str = ""
    added_at: float | None = None
    started_at: float | None = None
    finished_at: float | None = None
    detail: str = ""


class DispatchQueue(BaseModel):
    items: list[DispatchItem] = Field(default_factory=list)


class GatesResponse(BaseModel):
    repo: str = ""
    issue: int = 0
    ready: bool = False
    reasons: list[str] = Field(default_factory=list)
    gates: dict = Field(default_factory=dict)


class EnqueueRequest(BaseModel):
    repo: str = "remote-server"
    issue: object = None


class StartRequest(BaseModel):
    repo: str = "remote-server"
    issue: object = None
    intent: str = ""
    confirmed: bool = False


class StartResult(BaseModel):
    ok: bool = True
    repo: str = ""
    issue: int = 0
    status: str = "running"


class CancelRequest(BaseModel):
    repo: str = "remote-server"
    issue: object = None


class CancelResult(BaseModel):
    ok: bool = True
    removed: bool = False


@router.get("/health", response_model=DispatchHealth)
def health() -> dict:
    return {"ok": True}


@router.get("/queue", response_model=DispatchQueue)
def queue() -> dict:
    return {"items": lane_dispatch.queue_list()}


@router.get("/gates", response_model=GatesResponse)
def gates(repo: str = "remote-server", issue: str = "") -> dict:
    from backend.api.routers.issues import parse_issue_number
    from backend.core import validate_repo_name

    try:
        name = validate_repo_name(repo)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid repo")
    number = parse_issue_number(issue)
    if number is None:
        raise HTTPException(status_code=400, detail="invalid issue")
    ready = lane_dispatch.readiness(name, number)
    return {"repo": name, "issue": number, **ready}


@router.post("/enqueue", response_model=DispatchItem)
def enqueue(body: EnqueueRequest) -> dict:
    item, err = lane_dispatch.enqueue(body.repo, body.issue)
    if err is not None or item is None:
        raise HTTPException(status_code=400, detail=err or "bad item")
    return item


@router.post("/start", response_model=StartResult)
def start(body: StartRequest) -> dict:
    from backend.core import require_confirm

    require_confirm(body, START_INTENT)
    from backend.api.routers.issues import parse_issue_number
    from backend.core import validate_repo_name

    try:
        name = validate_repo_name(body.repo)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid repo")
    number = parse_issue_number(body.issue)
    if number is None:
        raise HTTPException(status_code=400, detail="invalid issue")
    item, err = lane_dispatch.dispatch(name, number)
    if err is not None or item is None:
        raise HTTPException(status_code=409, detail=err or "blocked")
    return {"ok": True, "repo": item["repo"], "issue": item["issue"],
            "status": "running"}


@router.post("/cancel", response_model=CancelResult)
def cancel(body: CancelRequest) -> dict:
    """Drop a queued/terminal item. No confirm: cancel stops work,
    same rule as the jobs run cancel."""
    from backend.api.routers.issues import parse_issue_number
    from backend.core import validate_repo_name

    try:
        name = validate_repo_name(body.repo)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid repo")
    number = parse_issue_number(body.issue)
    if number is None:
        raise HTTPException(status_code=400, detail="invalid issue")
    removed, err = lane_dispatch.cancel(name, number)
    if err == "item is running":
        raise HTTPException(status_code=409, detail=err)
    if err is not None:
        raise HTTPException(status_code=404, detail=err)
    return {"ok": True, "removed": removed}
