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

#: Step prefix a browser-drive op records on the shared control
#: state. The drawer banner keys off this prefix to show the agent
#: at the wheel. Plain string contract with CoBrowsingDrawer.tsx.
CHAT_DRIVE_PREFIX = "Browser drive:"


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
    "w:Math.round(r.width),h:Math.round(r.height),"
    "tid:e.getAttribute('data-testid'),"
    "id:e.id||null,name:e.getAttribute('name'),"
    "role:e.getAttribute('role'),"
    "cls:(typeof e.className==='string'?e.className:"
    "((e.className&&e.className.baseVal)||''))||null,"
    "val:('value' in e?e.value:null)}}}})()"
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
    return browser_view.remember_page(
        {
            "url": value.get("url", ""),
            "title": value.get("title", ""),
            "elements": value.get("els", []),
        }
    )


@router.get("/snapshot")
async def snapshot() -> dict[str, Any]:
    """Read-only view of the shared page: URL, title, click targets."""
    async with _drive_session() as (bridge, kind):
        page = await _snapshot(bridge)
    browser_view._log_activity(
        "snapshot",
        page["url"],
        via="agent",
        action="snapshot",
        url=page["url"],
        page_hash=browser_view.page_hash_of(page),
    )
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


@router.get("/notes")
async def notes() -> dict[str, Any]:
    """Shared browser notebook: auto-notes plus agent lessons.

    Read-only. The human can read everything the system has
    "learned" here; agents read it back at the start of each
    browsing turn, so corrections teach the next turn.
    """
    return {"ok": True, **browser_view.read_notes()}


@router.post("/learn")
async def learn() -> dict[str, Any]:
    """Run one learn pass now over events since the last pass.

    Same deterministic summarizer the automatic volume trigger
    uses — counts and destinations, no model, no spend. Returns
    what was written so the tab can show it immediately.
    """
    return {"ok": True, **browser_view.run_learn_pass(manual=True)}


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
        "learn": browser_view.learn_status(),
    }


@router.post("/drive")
async def drive(body: DriveRequest) -> dict[str, Any]:
    """Drive the shared page. Refuses while the human holds the wheel."""
    _require_human_not_driving()
    async with _drive_session() as (bridge, kind):
        # Pre-op page: selectors match what the action saw, not the
        # post-op snapshot taken below.
        pre_page, pre_hash = browser_view.last_page()
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
        selector = None
        if body.op in ("click", "wheel"):
            match = browser_view.match_target(pre_page, body.x, body.y)
            selector = browser_view.selector_for(match) if match else None
        browser_view._log_activity(
            "drive",
            f"{body.op} {target}",
            via="agent",
            action=body.op,
            selector=selector,
            x=body.x if body.op in ("click", "wheel") else None,
            y=body.y if body.op in ("click", "wheel") else None,
            url=(body.url if body.op == "navigate" else (pre_page or {}).get("url")),
            # A navigate's destination hash is unknown until the
            # post-op snapshot: never pair the new URL with the
            # previous page's hash.
            page_hash=(None if body.op == "navigate" else pre_hash),
        )
        if body.op == "navigate":
            # Fresh viewer sessions past the grace window restore the
            # last live page instead of the neutral start page — the
            # agent's navigations count, not just the human's.
            browser_view.remember_viewer_url(body.url)
        page = await _snapshot(bridge)
        # Presence for the drawer banner + a re-snapshot flag for any
        # later pipeline run: best-effort, the drive already landed.
        with contextlib.suppress(Exception):
            control = agent_control.read_control_state()
            control.current_step = f"{CHAT_DRIVE_PREFIX} {body.op} {target}"[:120]
            control.requires_dom_refresh = True
            agent_control.write_control_state(control)
    return {"ok": True, "op": body.op, "kind": kind, **page}
