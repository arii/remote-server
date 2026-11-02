"""system router: read-only box health."""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from backend.core import get_system_health

router = APIRouter(prefix="/api/system", tags=["system"])


class DiskInfo(BaseModel):
    total_gb: float | None = None
    used_gb: float | None = None
    free_gb: float | None = None
    percent: float | None = None
    summary: str | None = None
    error: str | None = None


class MemoryInfo(BaseModel):
    total_gb: float | None = None
    used_gb: float | None = None
    avail_gb: float | None = None
    percent: float | None = None
    summary: str | None = None
    error: str | None = None


class ContainerInfo(BaseModel):
    name: str = ""
    service: str = ""
    state: str = ""
    status: str = ""
    health: str = ""


class SystemHealth(BaseModel):
    disk: DiskInfo = Field(default_factory=DiskInfo)
    memory: MemoryInfo = Field(default_factory=MemoryInfo)
    containers: list[ContainerInfo] = Field(default_factory=list)


@router.get("/health", response_model=SystemHealth)
def health() -> dict:
    return get_system_health()
