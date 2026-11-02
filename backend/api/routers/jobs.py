"""jobs router: honest listing of backend-owned jobs.

The backend owns no job queue yet, so this endpoint honestly
returns an empty list. No fake data is ever served as real;
unwired actions do not exist on this router (no 501 needed).
"""

from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


class JobRecord(BaseModel):
    id: str
    kind: str = ""
    repo: str = ""
    done: bool = False


class JobList(BaseModel):
    jobs: list[JobRecord] = Field(default_factory=list)


class JobsHealth(BaseModel):
    ok: bool = True


@router.get("/health", response_model=JobsHealth)
def health() -> dict:
    return {"ok": True}


@router.get("", response_model=JobList)
def list_jobs() -> dict:
    return {"jobs": []}
