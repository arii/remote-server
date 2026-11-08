"""Input validation helpers (split from the backend.core god file)."""

from __future__ import annotations

from fastapi import HTTPException


REPO_NAME_RE = __import__("re").compile(r"^[A-Za-z0-9._-]+$")

BRANCH_NAME_RE = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]*$")


def validate_repo_name(repo: str) -> str:
    """Basename of a repo checkout; raises ValueError when invalid."""
    name = repo.strip().removeprefix("arii/").strip("/")
    if not name or "/" in name or not REPO_NAME_RE.match(name):
        raise ValueError(f"invalid repo name: {repo!r}")
    return name


def require_confirm(body, intent: str) -> None:
    """Confirm-tap guard for destructive routes.

    Raises HTTPException(400) unless the body carries
    {"intent": <intent>, "confirmed": True}. Accepts a dict or a
    pydantic model (or any object with intent/confirmed attrs).
    """
    if hasattr(body, "model_dump"):
        data = body.model_dump()
    elif isinstance(body, dict):
        data = body
    else:
        data = {
            "intent": getattr(body, "intent", None),
            "confirmed": getattr(body, "confirmed", None),
        }
    if not isinstance(data, dict) or data.get("intent") != intent \
            or data.get("confirmed") is not True:
        raise HTTPException(status_code=400, detail="confirmation required")
