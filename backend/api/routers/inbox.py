"""inbox router: aggregated triage inbox feed.

GET /api/inbox?repo= returns one ranked feed for a triage inbox:
{repo, updated_at, attention:[...], merge_ready:{count, top:[...]},
 runs:{active, recent:[...]}}.

Reuses existing seams only (no rewrite, no new deps):
- attention items: attention.attention (its own injectable seams)
- merge-ready PRs: pulls.prs rows filtered by merge_block_reason,
  a port of the frontend mergeBlockReason predicate
  (frontend/src/reviewTypes.ts)
- runs: the jobs registry (same records as GET /api/jobs)

All source readers are module-level functions so tests can
monkeypatch them (injectable seams).
"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.api.routers import attention, pulls
from backend.api.routers.attention import AttentionItem
from backend.api.routers.jobs import registry
from backend.core import validate_repo_name

router = APIRouter(prefix="/api", tags=["inbox"])

RECENT_RUNS_LIMIT = 5
MERGE_READY_TOP = 3


def list_attention_items(repo: str) -> list:
    """Injectable seam: attention items in ranked feed order."""
    return attention.attention(repo).get("items", [])


def list_open_prs(repo: str) -> list:
    """Injectable seam: open PR rows shaped by the pulls router."""
    return pulls.prs(repo).get("prs", [])


def list_runs() -> list:
    """Injectable seam: run records, newest first (jobs registry)."""
    with registry._lock:
        return sorted(registry.runs.values(),
                      key=lambda r: r.get("seq", 0), reverse=True)


def merge_block_reason(row: dict) -> str | None:
    """Port of frontend mergeBlockReason (reviewTypes.ts), applied
    to pulls.prs row dicts. Gates only on known-bad state."""
    if row.get("isDraft"):
        return "is a draft"
    review = row.get("reviewDecision") or ""
    if review != "APPROVED":
        return f"review is {review or 'unapproved'}"
    ci = row.get("ciStatus") or ""
    if ci != "passing":
        return f"CI is {ci or 'unknown'}"
    mergeable = row.get("mergeable") or ""
    if mergeable != "MERGEABLE":
        return f"merge state is {mergeable or 'unknown'}"
    return None


class MergeReadySummary(BaseModel):
    count: int = 0
    top: list = Field(default_factory=list)


class ActiveRun(BaseModel):
    id: str = ""
    action: str = ""
    started: float | None = None


class FinishedRun(BaseModel):
    id: str = ""
    action: str = ""
    exit: int | None = None


class RunsSummary(BaseModel):
    active: ActiveRun | None = None
    recent: list[FinishedRun] = Field(default_factory=list)


class InboxFeed(BaseModel):
    repo: str = ""
    updated_at: float = 0.0
    attention: list[AttentionItem] = Field(default_factory=list)
    merge_ready: MergeReadySummary = Field(
        default_factory=MergeReadySummary)
    runs: RunsSummary = Field(default_factory=RunsSummary)


@router.get("/inbox", response_model=InboxFeed)
def inbox(repo: str = "remote-server") -> dict:
    try:
        name = validate_repo_name(repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    items = list_attention_items(name)
    ready = [r for r in list_open_prs(name)
             if isinstance(r, dict) and merge_block_reason(r) is None]
    runs = [r for r in list_runs() if isinstance(r, dict)]
    active = None
    for r in runs:
        if not r.get("done"):
            active = {"id": r.get("id", ""),
                      "action": r.get("action", ""),
                      "started": r.get("started_at")}
            break
    recent = [{"id": r.get("id", ""), "action": r.get("action", ""),
               "exit": r.get("exit_code")}
              for r in runs if r.get("done")][:RECENT_RUNS_LIMIT]
    return {
        "repo": name,
        "updated_at": time.time(),
        "attention": items,
        "merge_ready": {
            "count": len(ready),
            "top": [{"number": r.get("number"),
                     "title": r.get("title", "")}
                    for r in ready[:MERGE_READY_TOP]],
        },
        "runs": {"active": active, "recent": recent},
    }
