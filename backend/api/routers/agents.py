"""agents router: agent runs listing + Pi session listing.

Runs shape: {"runs": [<runs.py summaries>]}, newest first, stable
run-<ledger-line> ids. Run detail adds the ledger entry plus its
trace events.
"""

import os
import sys

from fastapi import APIRouter
from pydantic import BaseModel, Field

from fastapi import HTTPException

from backend.agents.sessions import get_session_detail, list_sessions

sys.path.insert(0, os.path.join(
    os.path.dirname(
        os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "deep-agent"))

import runs  # noqa: E402

router = APIRouter(prefix="/api/agents", tags=["agents"])


class SessionRecord(BaseModel):
    file: str
    mtime: float
    cwd: str
    repo: str
    title: str
    id: str | None = None
    kind: str | None = None
    source: str | None = None
    done: bool | None = None


class SessionList(BaseModel):
    sessions: list[SessionRecord] = Field(default_factory=list)


class AgentsHealth(BaseModel):
    ok: bool = True


@router.get("/health", response_model=AgentsHealth)
def health() -> dict:
    return {"ok": True}


@router.get("/sessions", response_model=SessionList)
def sessions() -> dict:
    return {"sessions": list_sessions()}


@router.get("/sessions/{name}")
def session_detail(name: str) -> dict:
    try:
        return get_session_detail(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/runs")
def agent_runs(limit: int = 50) -> dict:
    return {"runs": runs.list_runs(limit)}


@router.get("/runs/{run_id}")
def agent_run(run_id: str) -> dict:
    row = runs.get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown run")
    return row
