"""Chat-driven shared-browser API — cdp-stream only.

lets chat turns (deep_chat browser_* tools) observe and drive the
exact page the human sees in the /cdp-stream viewer: attach to the
live shared Grid session, never mint a competing one (single slot),
and refuse mutating ops while the human holds the wheel
(agent_control PAUSED_BY_USER). BrowserMCP stays out: it is the
audit/test harness, not this product path.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.api.routers import agent_control, browser_view

router = APIRouter(prefix="/api/browser", tags=["browser_drive"])

# Cap the element list so a dense page cannot flood chat context.
MAX_ELEMENTS = 100

#: Step prefix a chat-driven op records on the shared control state.
#: The drawer banner keys off this prefix to show "agent driving
#: from chat". Plain string contract with CoBrowsingDrawer.tsx.
CHAT_DRIVE_PREFIX = "Chat drive:"


class DriveRequest(BaseModel):
    op: Literal["navigate", "click", "type", "key", "wheel"]
    url: str = ""
    x: int = 0
    y: int = 0
    dx: int = 0
    dy: int = 0
    text: str = ""
    key: str = ""
    code: str = ""


def _require_human_not_driving() -> None:
    state = agent_control.read_control_state()
    if state.state == "PAUSED_BY_USER":
        raise HTTPException(
            status_code=409,
            detail="you have the wheel — human is driving (agent paused)",
        )


@contextlib.asynccontextmanager
async def _drive_session():
    """Attach a bridge to the shared session. Never creates one.

    Agent sessions first (same precedence viewers use), else the
    refcounted viewer-shared session. Yields (bridge, kind); releases
    the viewer claim and closes the bridge on exit.
    """
    session_id = await asyncio.to_thread(browser_view._grid_live_session)
    kind = "agent"
    claimed: str | None = None
    if session_id is None:
        session_id = await asyncio.to_thread(browser_view._viewer_claim)
        if session_id is None:
            raise HTTPException(
                status_code=409,
                detail="browser busy — no shared session; "
                "open the drawer viewer and retry",
            )
        kind = "viewer-shared"
        claimed = session_id
    bridge = browser_view.CDPBridge(session_id)
    try:
        await bridge.connect()
    except Exception as exc:
        if claimed is not None:
            await asyncio.to_thread(browser_view._viewer_release, claimed)
        raise HTTPException(
            status_code=502, detail=f"browser connect failed: {exc}"
        ) from exc
    try:
        yield bridge, kind
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(bridge.close(), timeout=5)
        if claimed is not None:
            await asyncio.to_thread(browser_view._viewer_release, claimed)


SNAPSHOT_JS = (
    "(()=>{const els=[...document.querySelectorAll("
    "'a,button,input,textarea,select,[role=\"button\"],[onclick]')"
    "]"
    f".filter(e=>e.offsetParent!==null).slice(0,{MAX_ELEMENTS});"
    "return {url:document.URL,title:document.title,"
    "els:els.map((e,i)=>{const r=e.getBoundingClientRect();"
    "return {i,tag:e.tagName,"
    "text:(e.innerText||e.value||e.getAttribute('aria-label')||'')"
    ".slice(0,80),"
    "x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2),"
    "w:Math.round(r.width),h:Math.round(r.height)}})}})()"
)


async def _snapshot(bridge: browser_view.CDPBridge) -> dict[str, Any]:
    try:
        raw = await bridge.call(
            "Runtime.evaluate",
            {"expression": SNAPSHOT_JS, "returnByValue": True},
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"snapshot failed: {exc}") from exc
    value = (raw or {}).get("result", {}).get("value") or {}
    return {
        "url": value.get("url", ""),
        "title": value.get("title", ""),
        "elements": value.get("els", []),
    }


@router.get("/snapshot")
async def snapshot() -> dict[str, Any]:
    """Read-only view of the shared page: URL, title, click targets."""
    async with _drive_session() as (bridge, kind):
        page = await _snapshot(bridge)
    return {"ok": True, "kind": kind, **page}


@router.get("/activity")
async def activity(limit: int = 50) -> dict[str, Any]:
    """Shared input history, newest last, capped.

    Both sides learn from this: the human's viewer inputs and the
    agent's drive ops, each tagged with its origin. Read-only —
    observing while paused is fine (that is when the agent most
    needs to see what the human did).
    """
    return {"ok": True, "events": browser_view.recent_activity(limit)}


@router.get("/state")
async def drive_state() -> dict[str, Any]:
    """Rendezvous + wheel state without touching the browser."""
    control = agent_control.read_control_state()
    session_id = await asyncio.to_thread(browser_view._grid_live_session)
    shared: str | None = None
    if session_id is None:
        with contextlib.suppress(Exception):
            claimed = await asyncio.to_thread(browser_view._viewer_claim)
            if claimed is not None:
                await asyncio.to_thread(browser_view._viewer_release, claimed)
                shared = claimed[:8]
    return {
        "ok": True,
        "agent_session": (session_id[:8] if session_id else None),
        "viewer_shared_session": shared,
        "agent_state": control.state,
        "human_driving": control.state == "PAUSED_BY_USER",
    }


@router.post("/drive")
async def drive(body: DriveRequest) -> dict[str, Any]:
    """Drive the shared page. Refuses while the human holds the wheel."""
    _require_human_not_driving()
    async with _drive_session() as (bridge, kind):
        if body.op == "navigate":
            url = (body.url or "").strip()
            if not url.startswith(("http://", "https://")):
                raise HTTPException(
                    status_code=400,
                    detail="navigate needs an http(s) URL",
                )
            await bridge.navigate(url)
            # Let the load commit, then bind the possibly swapped page
            # target — same path the viewer takes after address-bar go.
            await asyncio.sleep(1.5)
            await bridge.reattach()
        elif body.op == "click":
            await bridge.click(body.x, body.y)
        elif body.op == "type":
            if not body.text:
                raise HTTPException(status_code=400, detail="type needs text")
            await bridge.type_text(body.text)
        elif body.op == "key":
            if not body.key:
                raise HTTPException(status_code=400, detail="key needs key")
            await bridge.key(body.key, body.code or body.key)
        elif body.op == "wheel":
            await bridge.wheel(body.x, body.y, body.dx, body.dy)
        target = (
            body.url
            if body.op == "navigate"
            else (
                f"{body.x},{body.y}"
                if body.op in ("click", "wheel")
                else (body.text or body.key or body.op)
            )
        )
        browser_view._log_activity("drive", f"{body.op} {target}", via="agent")
        page = await _snapshot(bridge)
        # Presence for the drawer banner + a re-snapshot flag for any
        # later pipeline run: best-effort, the drive already landed.
        with contextlib.suppress(Exception):
            control = agent_control.read_control_state()
            control.current_step = f"{CHAT_DRIVE_PREFIX} {body.op} {target}"[:120]
            control.requires_dom_refresh = True
            agent_control.write_control_state(control)
    return {"ok": True, "op": body.op, "kind": kind, **page}
