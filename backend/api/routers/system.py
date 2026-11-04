"""system router: read-only box health."""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from backend.core import get_container_stats, get_system_health

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
    containers_error: str | None = None


@router.get("/health", response_model=SystemHealth)
def health() -> dict:
    return get_system_health()


class ContainerStats(BaseModel):
    name: str = ""
    service: str = ""
    state: str = ""
    cpu_pct: float | None = None
    mem_used: str | None = None
    mem_limit: str | None = None


class SystemStats(BaseModel):
    containers: list[ContainerStats] = Field(default_factory=list)


@router.get("/stats", response_model=SystemStats)
def stats() -> dict:
    return get_container_stats()
