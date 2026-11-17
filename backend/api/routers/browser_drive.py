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
import base64
import contextlib
import time
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend import skill_runs
from backend.api.routers import agent_control, browser_ext, browser_view
from backend.browser_safety import (
    HumanWheelPaused,
    require_human_not_driving as _require_human_not_driving,
)
from backend.skill_trace import finish_step, page_hash_of, sensitive_element, step_trace

router = APIRouter(prefix="/api/browser", tags=["browser_drive"])

# Cap the element list so a dense page cannot flood chat context.
MAX_ELEMENTS = 100
LOAD_TIMEOUT_SECONDS = 15
LOAD_POLL_SECONDS = 0.1
OBSERVED_TEXT_LIMIT = 8000

#: Step prefix a browser-drive op records on the shared control
#: state. The drawer banner keys off this prefix to show the agent
#: at the wheel. Plain string contract with CoBrowsingDrawer.tsx.
CHAT_DRIVE_PREFIX = "Browser drive:"
DriveTarget = Literal["grid", "paired"]


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


@contextlib.asynccontextmanager
async def _drive_session(target: DriveTarget = "grid"):
    """Attach a bridge to the shared session. Never creates one.

    Agent sessions first (same precedence viewers use), else the
    refcounted viewer-shared session. Yields (bridge, kind); releases
    the viewer claim and closes the bridge on exit.
    """
    if target == "paired":
        bridge: browser_view.CDPBridge = browser_ext.paired_bridge()
        try:
            await bridge.connect()
            yield bridge, "paired"
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"paired Chrome failed: {exc}"
            ) from exc
        return
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
    "const tokens=(e.getAttribute('autocomplete')||'').toLowerCase().split(/\\s+/);"
    "const sensitive=e.matches('input[type=\"password\"]')||tokens.some(t=>"
    "['username','current-password','new-password','one-time-code','webauthn'].includes(t));"
    "const text=e.innerText||e.value||e.getAttribute('aria-label')||'';"
    "return {i,tag:e.tagName,"
    "sensitive,focused:e===document.activeElement,type:e.type||null,"
    "text:sensitive?(text?'[redacted]':''):text.slice(0,80),"
    "x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2),"
    "w:Math.round(r.width),h:Math.round(r.height),"
    "tid:e.getAttribute('data-testid'),"
    "id:e.id||null,name:e.getAttribute('name'),"
    "role:e.getAttribute('role'),"
    "cls:(typeof e.className==='string'?e.className:"
    "((e.className&&e.className.baseVal)||''))||null,"
    "val:('value' in e?(sensitive&&e.value?'[redacted]':e.value):null)};})};})()"
)


def snapshot_from_cdp(raw: dict[str, Any]) -> dict[str, Any]:
    if "exceptionDetails" in raw:
        details = raw["exceptionDetails"]
        message = details.get("exception", {}).get("description") or details["text"]
        raise ValueError(f"snapshot evaluation failed: {message}")
    value = raw["result"]["value"]
    return {
        "url": value["url"],
        "title": value["title"],
        "elements": value["els"],
    }


async def _snapshot(
    bridge: browser_view.CDPBridge, target: DriveTarget = "grid"
) -> dict[str, Any]:
    try:
        raw = await bridge.call(
            "Runtime.evaluate",
            {"expression": SNAPSHOT_JS, "returnByValue": True},
        )
        page = snapshot_from_cdp(raw)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"snapshot failed: {exc}") from exc
    if target == "paired":
        assert isinstance(bridge, browser_ext.PairedBridge)
        bridge.url, bridge.title = page["url"], page["title"]
        return browser_view.remember_page(page, target=target)
    return browser_view.remember_page(page)


async def capture(
    target: DriveTarget = "grid", start_url: str | None = None
) -> tuple[dict, bytes]:
    """Capture masked observations and a PNG through either CDP transport."""
    async with _drive_session(target) as (bridge, _kind):
        _require_human_not_driving()
        loader = None
        if start_url:
            if urlsplit(start_url).scheme not in ("http", "https"):
                raise ValueError("start_url needs an http(s) URL")
            navigation = await bridge.call("Page.navigate", {"url": start_url})
            if navigation.get("errorText"):
                raise RuntimeError(f"navigation failed: {navigation['errorText']}")
            await bridge.reattach()
            loader = navigation.get("loaderId")
        deadline = time.monotonic() + LOAD_TIMEOUT_SECONDS
        while True:
            _require_human_not_driving()
            committed = True
            if loader:
                tree = await bridge.call("Page.getFrameTree")
                committed = tree["frameTree"]["frame"].get("loaderId") == loader
            ready = await bridge.call(
                "Runtime.evaluate",
                {"expression": "document.readyState", "returnByValue": True},
            )
            if committed and ready.get("result", {}).get("value") == "complete":
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("browser page load timed out")
            await asyncio.sleep(LOAD_POLL_SECONDS)
        page = await _snapshot(bridge, target)
        password = await bridge.call(
            "Runtime.evaluate",
            {
                "expression": "!!document.querySelector('input[type=\"password\"]')",
                "returnByValue": True,
            },
        )
        if "exceptionDetails" in password or not isinstance(
            password.get("result", {}).get("value"), bool
        ):
            raise RuntimeError("could not inspect login fields")
        screenshot = await bridge.call("Page.captureScreenshot", {"format": "png"})
        png = base64.b64decode(screenshot["data"], validate=True)
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("browser returned an invalid PNG")
        parsed = urlsplit(page["url"])
        login_url = parsed.hostname == "github.com" and (
            parsed.path == "/login" or parsed.path.startswith("/login/")
        )
        return {
            "url": page["url"],
            "title": page["title"],
            "observed_text": "\n".join(
                str(el.get("text", "")) for el in page["elements"]
            )[:OBSERVED_TEXT_LIMIT],
            "landed_on_login": login_url or password["result"]["value"],
        }, png


@router.get("/snapshot")
async def snapshot(target: DriveTarget = "grid") -> dict[str, Any]:
    """Read-only view of the shared page: URL, title, click targets."""
    async with _drive_session(target) as (bridge, kind):
        page = await _snapshot(bridge, target)
    browser_view._log_activity(
        "snapshot",
        page["url"],
        via="agent",
        action="snapshot",
        url=page["url"],
        page_hash=browser_view.page_hash_of(page),
        target=target,
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
async def drive_state(target: DriveTarget = "grid") -> dict[str, Any]:
    """Rendezvous + wheel state without touching the browser."""
    control = agent_control.read_control_state()
    session_id = (
        await asyncio.to_thread(browser_view._grid_live_session)
        if target == "grid"
        else None
    )
    shared: str | None = None
    if target == "grid" and session_id is None:
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
        "paired": browser_ext.paired_state(),
    }


@router.post("/drive")
async def drive(body: DriveRequest, target: DriveTarget = "grid") -> dict[str, Any]:
    """Drive the shared page. Refuses while the human holds the wheel."""
    page, _ = browser_view.last_page(target)
    match = browser_view.match_target(page, body.x, body.y)
    proposal = {
        "action": body.op,
        "selector": body.url
        if body.op == "navigate"
        else (browser_view.selector_for(match) if match else ""),
        "value": body.url if body.op == "navigate" else body.text or body.key,
        "x": body.x,
        "y": body.y,
        "dx": body.dx,
        "dy": body.dy,
        "code": body.code,
    }
    context = {
        "target": target,
        "mode": "live",
        "source_url": (page or {}).get("url", body.url),
    }
    trace = step_trace(1, proposal, page=page, mode="live", context=context)
    trace["context"]["page_hash_phase"] = "cached_before" if page else "unavailable"
    if body.op in ("type", "key"):
        trace["sensitive"] = True
    try:
        result = await _execute_drive(body, target, trace)
        if "url" in result:
            trace["context"]["post_page_hash"] = page_hash_of(result)
            if trace["page_hash"] is None:
                trace["page_hash"] = page_hash_of(result)
                trace["context"].update(page_hash_phase="after", url=result["url"])
        elif "dialog" in result:
            trace["context"]["dialog_type"] = result["dialog"]["type"]
            if trace["page_hash"] is None:
                trace["context"]["page_hash_unavailable"] = "dialog"
        finish_step(trace, "passed")
        return result
    except HumanWheelPaused as exc:
        trace["executed"] = False
        finish_step(trace, "blocked", str(exc.detail))
        raise
    except HTTPException as exc:
        finish_step(trace, "error", str(exc.detail))
        raise
    except Exception as exc:
        finish_step(trace, "error", f"driver failed: {exc}")
        raise
    finally:
        skill_runs.record_run(
            {
                "trace_version": 1,
                "skill": "live-drive",
                "version": 1,
                "context": trace["context"],
                "passed": trace.get("ok", False),
                "failed_step": None if trace.get("ok") else 1,
                "reason": trace.get("reason", ""),
                "steps": [trace],
            }
        )


async def _execute_drive(
    body: DriveRequest, target: DriveTarget, trace: dict
) -> dict[str, Any]:
    _require_human_not_driving()
    async with _drive_session(target) as (bridge, kind):
        # Pre-op page: selectors match what the action saw, not the
        # post-op snapshot taken below.
        if target == "paired":
            _require_human_not_driving()
        pre_page, pre_hash = browser_view.last_page(target)
        sensitive = False
        if body.op in ("type", "key"):
            match = browser_view.match_target(pre_page, body.x, body.y)
            candidates = [
                el for el in (pre_page or {}).get("elements", []) if el.get("focused")
            ]
            if match:
                candidates.append(match)
            sensitive = any(
                sensitive_element(el) for el in candidates
            ) or await browser_view.credential_focused(bridge)
            trace["sensitive"] = sensitive
        if body.op == "navigate" and not (body.url or "").strip().startswith(
            ("http://", "https://")
        ):
            raise HTTPException(status_code=400, detail="navigate needs an http(s) URL")
        if body.op == "type" and not body.text:
            raise HTTPException(status_code=400, detail="type needs text")
        if body.op == "key" and not body.key:
            raise HTTPException(status_code=400, detail="key needs key")
        _require_human_not_driving()
        trace["executed"] = True
        if body.op == "navigate":
            url = (body.url or "").strip()
            await bridge.navigate(url)
            # Let the load commit, then bind the possibly swapped page
            # target — same path the viewer takes after address-bar go.
            await asyncio.sleep(1.5)
            await bridge.reattach()
        elif body.op == "click":
            await bridge.click(body.x, body.y)
        elif body.op == "type":
            await bridge.type_text(body.text)
        elif body.op == "key":
            await bridge.key(body.key, body.code or body.key)
        elif body.op == "wheel":
            await bridge.wheel(body.x, body.y, body.dx, body.dy)
        detail = (
            body.url
            if body.op == "navigate"
            else (
                f"{body.x},{body.y}"
                if body.op in ("click", "wheel")
                else ("[redacted]" if sensitive else body.text or body.key or body.op)
            )
        )
        selector = None
        if body.op in ("click", "wheel"):
            match = browser_view.match_target(pre_page, body.x, body.y)
            selector = browser_view.selector_for(match) if match else None
        browser_view._log_activity(
            "drive",
            f"{body.op} {detail}",
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
            target=target,
        )
        result = {"ok": True, "op": body.op, "kind": kind}
        if bridge.dialog is not None:
            result["dialog"] = dict(bridge.dialog)
        else:
            result.update(await _snapshot(bridge, target))
        # Presence for the drawer banner + a re-snapshot flag for any
        # later pipeline run: best-effort, the drive already landed.
        with contextlib.suppress(Exception):
            control = agent_control.read_control_state()
            control.current_step = f"{CHAT_DRIVE_PREFIX} {body.op} {detail}"[:120]
            control.requires_dom_refresh = True
            agent_control.write_control_state(control)
    return result
