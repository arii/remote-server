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


def latest_webhook_success_ts() -> float:
    """Injectable seam: newest successful webhook ts (0.0 if none)."""
    try:
        entries = ledger.read(kind="webhook", limit=200)
    except Exception:
        return 0.0
    best = 0.0
    for e in entries:
        if isinstance(e, dict) and e.get("ok") is True:
            try:
                best = max(best, float(e.get("ts", 0) or 0))
            except (TypeError, ValueError):
                pass
    return best


def list_failed_webhooks() -> list:
    """Injectable seam: failed webhooks from ledger kinds.

    Failures older than the latest success are resolved history,
    not queue items — a later good pull means the checkout moved on.
    """
    resolved_before = latest_webhook_success_ts()
    entries = ledger.read(kind="webhook", limit=50)
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        try:
            ts = float(e.get("ts", 0) or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts <= resolved_before:
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
    seen_runs: dict[tuple[str, str], dict] = {}
    for wf in (health.get("workflows") or []):
        if not isinstance(wf, dict):
            continue
        rid = wf.get("id", "?")
        title = f"Failing workflow: {wf.get('name', 'Workflow')}"
        detail = wf.get("diagnosed_cause", "Workflow failed.")
        key = (title, detail)
        if key in seen_runs:
            seen_runs[key]["runs"] += 1
            continue
        item = {
            "id": f"run-{rid}",
            "kind": "failing-run",
            "title": title,
            "detail": detail,
            "repo": name,
            "severity": "crit",
            "action": {"label": "View health", "method": "GET",
                       "href": f"api/workflow-health?repo={name}",
                       "destructive": False, "intent": ""},
            "created_at": now,
        }
        seen_runs[key] = {"item": item, "runs": 1}
        items.append(item)
    for entry in seen_runs.values():
        if entry["runs"] > 1:
            entry["item"]["title"] += f" ×{entry['runs']}"
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
                       "href": "api/jobs", "destructive": False, "intent": ""},
            "created_at": float(job.get("created_at", now) or now),
        })
    # One row per incident: repeat failures with the same repo+detail
    # collapse into a single item with a count (latest first).
    webhook_incidents: dict = {}
    for e in list_failed_webhooks():
        repo = str(e.get("repo", name))
        detail = str(e.get("error", e.get("event", "webhook failed")))
        key = (repo, detail)
        ts = float(e.get("ts", now) or now)
        prev = webhook_incidents.get(key)
        if prev is None or ts >= prev["created_at"]:
            eid = e.get("id", e.get("ts", "?"))
            webhook_incidents[key] = {
                "id": f"webhook-{eid}",
                "kind": "failed-webhook",
                "title": "Failed webhook delivery",
                "detail": detail,
                "repo": repo,
                "severity": "warn",
                "action": {"label": "View notifications", "method": "GET",
                           "href": "api/notifications/status",
                           "destructive": False, "intent": ""},
                "created_at": ts,
                "count": (prev["count"] + 1) if prev else 1,
            }
        else:
            prev["count"] += 1
    for item in webhook_incidents.values():
        if item["count"] > 1:
            item["title"] += f" (×{item['count']})"
        del item["count"]
        items.append(item)
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
                       "href": "api/prs?repo=" + name,
                       "destructive": False, "intent": ""},
            "created_at": float(e.get("ts", e.get("created_at", now)) or now),
        })
    return {"updated_at": now, "items": items}
