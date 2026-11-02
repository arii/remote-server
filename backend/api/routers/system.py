"""system router stub."""

from fastapi import APIRouter

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/health")
def health() -> dict:
    return {"ok": True}
