"""Notify router: webhook + notifications endpoints.

Same four routes/paths/verbs as buttons/server.py:
  POST /api/webhook
  GET  /api/notifications/status
  POST /api/notifications/test
  POST /api/notifications/trigger-digest
"""

from __future__ import annotations

import json
import threading
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend import notify as N

router = APIRouter(tags=["notify"])


class NotificationsStatus(BaseModel):
    ntfy_configured: bool = False
    topic: str = ""
    webhook_configured: bool = False
    digest_interval_hours: float = 12.0
    last_digest: float = 0
    enabled_events: list[str] = Field(default_factory=list)


class NotificationResult(BaseModel):
    ok: bool = True
    results: dict = Field(default_factory=dict)


class DigestResult(BaseModel):
    ok: bool = True
    results: dict = Field(default_factory=dict)
    digest: str = ""


def _resolve_repo_path(repo_full_name: str):
    """Map a GitHub full_name to a local checkout path, or None."""
    import os
    short = (repo_full_name or "").split("/")[-1].strip()
    if not short:
        return None
    # The stack checkout itself lives outside the repos dir.
    stack = os.environ.get("BUTTONS_STACK", "/home/ari/remote-server")
    if short == os.path.basename(stack.rstrip("/")) \
            and os.path.isdir(stack):
        return stack
    try:
        from backend.core import resolve_repo_path
        path = resolve_repo_path(short)
    except Exception:
        return None
    return str(path) if os.path.isdir(str(path)) else None


def _queue_pull(repo: str, path: str):
    with N.webhook_state_lock:
        if repo in N.webhook_pending:
            return False
        N.webhook_pending.add(repo)
    threading.Thread(target=N.webhook_worker, args=(repo, path),
                     daemon=True).start()
    return True


@router.post("/api/webhook")
async def github_webhook(request: Request):
    secret = N.webhook_secret()
    if not secret:
        return JSONResponse(
            {"error": "webhook not configured (WEBHOOK_SECRET unset)"},
            status_code=503)
    body = await request.body()
    if len(body) > N.WEBHOOK_MAX_BODY:
        return JSONResponse({"error": "payload too large"}, status_code=413)
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not N.verify_signature(secret, body, sig):
        return JSONResponse({"error": "bad signature"}, status_code=401)
    event = request.headers.get("X-GitHub-Event", "")
    try:
        payload = json.loads(body) if body else {}
    except Exception:
        if event == "ping":
            payload = {}
        else:
            return JSONResponse({"error": "bad JSON"}, status_code=400)
    repo = (payload.get("repository") or {}).get("full_name")

    if event == "ping":
        path = _resolve_repo_path(repo or "")
        if path:
            _queue_pull(repo, path)
        return {"ok": True, "msg": "pong"}
    if event != "push":
        return {"ignored": event}
    path = _resolve_repo_path(repo or "")
    if not path:
        return {"ignored": repo}
    if payload.get("ref") != "refs/heads/main":
        return {"skipped": "not main"}
    if not _queue_pull(repo, path):
        return {"queued": True, "coalesced": True}
    return JSONResponse({"queued": True, "repo": repo}, status_code=202)


@router.get("/api/notifications/status",
            response_model=NotificationsStatus)
def notifications_status() -> dict:
    import os
    ntfy_topic = os.environ.get("NTFY_TOPIC", "").strip()
    webhook_url = os.environ.get("NOTIFICATION_WEBHOOK_URL", "").strip()
    state = N.load_notifier_state()
    return {
        "ntfy_configured": bool(ntfy_topic),
        "topic": ntfy_topic,
        "webhook_configured": bool(webhook_url),
        "digest_interval_hours": N.digest_interval_hours(),
        "last_digest": state.get("last_digest", 0),
        "enabled_events": N.enabled_events(),
    }


@router.post("/api/notifications/test",
             response_model=NotificationResult)
def notifications_test() -> dict:
    res = N.send_notification(
        "Test Notification", "Test push from S5 backend.",
        priority="default", tags=["test"], event_type="test")
    return {"ok": True, "results": res}


@router.post("/api/notifications/trigger-digest",
             response_model=DigestResult)
def notifications_trigger_digest() -> dict:
    digest_msg = N.build_digest_text()
    res = N.send_notification(
        "Manual Briefing Digest", digest_msg, priority="low",
        tags=["clipboard", "digest"], event_type="digest")
    with N.notifier_lock:
        state = N.load_notifier_state()
        state["last_digest"] = time.time()
        N.save_notifier_state(state)
    return {"ok": True, "results": res, "digest": digest_msg}
