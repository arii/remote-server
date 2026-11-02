"""repo router stub."""

from fastapi import APIRouter

router = APIRouter(prefix="/api/repo", tags=["repo"])


@router.get("/health")
def health() -> dict:
    return {"ok": True}
