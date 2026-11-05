"""repo router: git status/diff reads + confirmed one-click actions."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.core import (get_repo_diff, get_repo_metrics,
                          get_repo_status, list_repos,
                          list_repo_files, repo_discard_untracked,
                          repo_stash, repo_sync, require_confirm)

router = APIRouter(prefix="/api/repo", tags=["repo"])

repos_router = APIRouter(prefix="/api/repos", tags=["repos"])


class RepoStatus(BaseModel):
    repo: str
    branch: str
    short: str
    clean: bool


class RepoDiffFile(BaseModel):
    path: str
    status: str
    staged: bool = False
    unstaged: bool = False
    additions: int = 0
    deletions: int = 0


class RepoDiff(BaseModel):
    repo: str
    branch: str
    clean: bool
    status_short: str
    status_truncated: bool = False
    diff_stat: str
    diff: str
    truncated: bool
    files: list[RepoDiffFile] = []
    files_truncated: bool = False


class RepoActionRequest(BaseModel):
    repo: str = "remote-server"
    action: str = ""
    intent: str = ""
    confirmed: bool = False


class RepoActionResult(BaseModel):
    ok: bool = True
    repo: str = ""
    action: str = ""
    clean: bool = False
    detail: str = ""


REPO_ACTIONS = ("stash", "discard-untracked", "sync")


class RepoHealth(BaseModel):
    ok: bool = True


class TodoHit(BaseModel):
    file: str
    line: int
    text: str


class RepoMetrics(BaseModel):
    repo: str
    branch: str
    clean: bool
    status_count: int
    todo_count: int
    todo_hits: list[TodoHit]
    diff_stat: str
    diff_files: int


class RepoEntry(BaseModel):
    name: str
    full: str
    branch: str
    clean: bool
    # Workspace lag overview (#937): branch-header counts plus the
    # dirty-entry count backing the Now dirty-repos strip.
    ahead: int = 0
    behind: int = 0
    status_count: int = 0


class ReposList(BaseModel):
    repos: list[RepoEntry]


class RepoFiles(BaseModel):
    repo: str
    prefix: str
    files: list[str]
    truncated: bool


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


@router.post("/action", response_model=RepoActionResult)
def repo_action(body: RepoActionRequest) -> dict:
    if body.action not in REPO_ACTIONS:
        raise HTTPException(status_code=400, detail="unknown action")
    require_confirm(body, "repo-action")
    try:
        if body.action == "stash":
            return repo_stash(body.repo)
        if body.action == "discard-untracked":
            return repo_discard_untracked(body.repo)
        return repo_sync(body.repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/metrics", response_model=RepoMetrics)
def metrics(repo: str) -> dict:
    try:
        return get_repo_metrics(repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/files", response_model=RepoFiles)
def files(repo: str, prefix: str = "") -> dict:
    try:
        return list_repo_files(repo, prefix)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
