"""attention router: aggregated attention queue.

GET /api/attention?repo= returns {updated_at, items: [...]} where each
item is {id, kind, title, detail, repo, severity, action, created_at}.
Sources (existing helpers only, no new infra):
- failing runs: ops.fetch_workflow_health (workflow-health logic)
- failed jobs: jobs registry (non-ok/failed/terminal-error records)
- failed webhooks: ledger kind-filtered reads
- pending approvals: ledger + jobs-registry pending records

Empty items = all-clear. All source readers are module-level
functions so tests can monkeypatch them (injectable seams).
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend import ledger
from backend.api.routers import ops
from backend.api.routers.jobs import registry
from backend.core import validate_repo_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["attention"])


class Action(BaseModel):
    label: str = ""
    method: str = "GET"
    href: str = ""
    destructive: bool = False
    intent: str = ""


class AttentionItem(BaseModel):
    id: str = ""
    kind: str = ""
    title: str = ""
    detail: str = ""
    repo: str = ""
    severity: str = "info"
    action: Action = Field(default_factory=Action)
    created_at: float = 0.0


class AttentionList(BaseModel):
    updated_at: float = 0.0
    items: list[AttentionItem] = Field(default_factory=list)


# TTL cache: the gh subprocess chain behind workflow-health takes
# seconds; the Now queue polls it on every view. Freshness of a minute
# is plenty for an attention list. Tests reset _WF_CACHE directly.
WF_CACHE_TTL = 60.0
_WF_CACHE: dict[str, tuple[float, dict]] = {}


def get_workflow_health(repo: str) -> dict:
    """Injectable seam: failing-run diagnosis via workflow-health logic."""
    hit = _WF_CACHE.get(repo)
    if hit is not None and time.time() - hit[0] < WF_CACHE_TTL:
        return hit[1]
    data = ops.fetch_workflow_health(repo, f"arii/{repo}") or {}
    _WF_CACHE[repo] = (time.time(), data)
    return data


def list_failed_jobs() -> list:
    """Injectable seam: failed jobs from the jobs registry."""
    out = []
    for job in registry.list_jobs():
        if not isinstance(job, dict):
            continue
        state = str(job.get("state", "")).lower()
        status = str(job.get("status", "")).lower()
        if state in ("failed", "error") or status in ("failed", "error"):
            out.append(job)
        elif job.get("done") is True and job.get("exit_code") not in (None, 0):
            out.append(job)
    return out


def list_failed_webhooks() -> list:
    """Injectable seam: failed webhooks from ledger kinds."""
    entries = ledger.read(kind="webhook", limit=50)
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        blob = " ".join(str(e.get(k, "")) for k in
                        ("event", "status", "error", "detail")).lower()
        if "fail" in blob or "error" in blob or e.get("ok") is False \
                or e.get("status") in ("failed", "error"):
            out.append(e)
    return out


def list_pending_approvals() -> list:
    """Injectable seam: pending approvals (ledger + jobs registry)."""
    out = []
    for e in ledger.read(kind="all", limit=100):
        if not isinstance(e, dict):
            continue
        if e.get("awaiting_approval") is True or e.get("pending_approval") is True \
                or str(e.get("event", "")).lower() in (
                    "approval requested", "approval pending", "awaiting approval"):
            out.append(e)
    try:
        for job in registry.list_jobs():
            if isinstance(job, dict) and job.get("awaiting_approval") is True:
                out.append(job)
    except Exception as exc:
        logger.warning("attention: jobs registry read failed: %s", exc)
    return out


@router.get("/attention", response_model=AttentionList)
def attention(repo: str = "remote-server") -> dict:
    try:
        name = validate_repo_name(repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    now = time.time()
    items: list[dict] = []
    try:
        health = get_workflow_health(name) or {}
    except Exception as exc:
        logger.warning("attention: workflow-health read failed: %s", exc)
        health = {}
    for wf in (health.get("workflows") or []):
        if not isinstance(wf, dict):
            continue
        rid = wf.get("id", "?")
        items.append({
            "id": f"run-{rid}",
            "kind": "failing-run",
            "title": f"Failing workflow: {wf.get('name', 'Workflow')}",
            "detail": wf.get("diagnosed_cause", "Workflow failed."),
            "repo": name,
            "severity": "crit",
            "action": {"label": "View health", "method": "GET",
                       "href": f"/api/workflow-health?repo={name}",
                       "destructive": False, "intent": ""},
            "created_at": now,
        })
    for job in list_failed_jobs():
        jid = job.get("id", "?")
        items.append({
            "id": f"job-{jid}",
            "kind": "failed-job",
            "title": f"Failed job: {job.get('name', jid)}",
            "detail": str(job.get("error", job.get("detail", "job failed"))),
            "repo": str(job.get("repo", name)),
            "severity": "warn",
            "action": {"label": "View jobs", "method": "GET",
                       "href": "/api/jobs", "destructive": False, "intent": ""},
            "created_at": float(job.get("created_at", now) or now),
        })
    for e in list_failed_webhooks():
        eid = e.get("id", e.get("ts", "?"))
        items.append({
            "id": f"webhook-{eid}",
            "kind": "failed-webhook",
            "title": "Failed webhook delivery",
            "detail": str(e.get("error", e.get("event", "webhook failed"))),
            "repo": str(e.get("repo", name)),
            "severity": "warn",
            "action": {"label": "View notifications", "method": "GET",
                       "href": "/api/notifications/status",
                       "destructive": False, "intent": ""},
            "created_at": float(e.get("ts", now) or now),
        })
    for e in list_pending_approvals():
        eid = e.get("id", e.get("ts", "?"))
        items.append({
            "id": f"approval-{eid}",
            "kind": "pending-approval",
            "title": str(e.get("title", "Approval pending")),
            "detail": str(e.get("detail", e.get("event", "awaiting approval"))),
            "repo": str(e.get("repo", name)),
            "severity": "info",
            "action": {"label": "Review", "method": "GET",
                       "href": "/api/prs?repo=" + name,
                       "destructive": False, "intent": ""},
            "created_at": float(e.get("ts", e.get("created_at", now)) or now),
        })
    return {"updated_at": now, "items": items}
