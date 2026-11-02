"""agents router stub."""

from fastapi import APIRouter

router = APIRouter(prefix="/api/agents", tags=["agents"])


@router.get("/health")
def health() -> dict:
    return {"ok": True}
