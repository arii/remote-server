"""jobs router stub."""

from fastapi import APIRouter

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("/health")
def health() -> dict:
    return {"ok": True}
