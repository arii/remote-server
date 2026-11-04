"""repo router: read-only git status for one validated repo."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.core import get_repo_diff, get_repo_status, list_repos

router = APIRouter(prefix="/api/repo", tags=["repo"])

repos_router = APIRouter(prefix="/api/repos", tags=["repos"])


class RepoStatus(BaseModel):
    repo: str
    branch: str
    short: str
    clean: bool


class RepoDiff(BaseModel):
    repo: str
    branch: str
    clean: bool
    status_short: str
    diff_stat: str
    diff: str
    truncated: bool


class RepoHealth(BaseModel):
    ok: bool = True


class RepoEntry(BaseModel):
    name: str
    full: str
    branch: str
    clean: bool


class ReposList(BaseModel):
    repos: list[RepoEntry]


@router.get("/health", response_model=RepoHealth)
def health() -> dict:
    return {"ok": True}


@repos_router.get("", response_model=ReposList)
def repos_list() -> dict:
    return {"repos": list_repos()}


@repos_router.get("/", response_model=ReposList, include_in_schema=False)
def repos_list_slash() -> dict:
    return {"repos": list_repos()}


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


@router.get("/diff", response_model=RepoDiff)
def diff(repo: str) -> dict:
    try:
        return get_repo_diff(repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
