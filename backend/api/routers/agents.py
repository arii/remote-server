"""agents router: read-only Pi session listing.

List shape follows docs/session-record-contract.md list response:
{"sessions": [<pi-file rows>]}, newest first by mtime. Only
transcript files on disk are listed (no background/ledger rows:
this backend owns no runs and adds no Jules surface).
"""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from fastapi import HTTPException

from backend.agents.sessions import get_session_detail, list_sessions

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
