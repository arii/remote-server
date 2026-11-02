"""repo router: read-only git status for one validated repo."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.core import get_repo_status

router = APIRouter(prefix="/api/repo", tags=["repo"])


class RepoStatus(BaseModel):
    repo: str
    branch: str
    short: str
    clean: bool


class RepoHealth(BaseModel):
    ok: bool = True


@router.get("/health", response_model=RepoHealth)
def health() -> dict:
    return {"ok": True}


@router.get("/status", response_model=RepoStatus)
def status(repo: str) -> dict:
    try:
        return get_repo_status(repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
