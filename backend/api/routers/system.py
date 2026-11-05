"""system router: read-only box health + per-container ops (#928)."""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from backend.core import (
    CONTAINER_LOGS_TAIL_MAX,
    get_container_logs,
    get_container_stats,
    get_system_health,
    require_confirm,
    restart_container,
)
from backend.ledger import append as ledger_append

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
    status: str = ""
    health: str = ""
    cpu_pct: float | None = None
    mem_used: str | None = None
    mem_limit: str | None = None


class SystemStats(BaseModel):
    containers: list[ContainerStats] = Field(default_factory=list)


@router.get("/stats", response_model=SystemStats)
def stats() -> dict:
    return get_container_stats()


class ContainerLogs(BaseModel):
    service: str = ""
    name: str = ""
    lines: list[str] = Field(default_factory=list)
    truncated: bool = False


class RestartRequest(BaseModel):
    intent: str = ""
    confirmed: bool = False


class RestartResult(BaseModel):
    ok: bool = True
    service: str = ""


@router.get("/containers/{service}/logs", response_model=ContainerLogs)
def container_logs(
    service: str,
    tail: int = Query(default=100, ge=1, le=CONTAINER_LOGS_TAIL_MAX),
) -> dict:
    try:
        return get_container_logs(service, tail=tail)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError:
        raise HTTPException(
            status_code=404, detail=f"unknown container: {service!r}")
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:500])


@router.post("/containers/{service}/restart", response_model=RestartResult)
def container_restart(service: str, body: RestartRequest) -> dict:
    require_confirm(body, "container-restart")
    try:
        result = restart_container(service)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError:
        raise HTTPException(
            status_code=404, detail=f"unknown container: {service!r}")
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:500])
    ledger_append({"kind": "system",
                   "event": f"container {service} restarted"})
    return result
