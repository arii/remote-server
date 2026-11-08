"""CDP-direct shared-autonomy viewer (no VNC in the loop).

Streams the headed Grid browser to a lightweight canvas viewer over one
websocket and relays human input back as CDP Input events. Mounted at
/cdp-stream in backend/server.py. One viewer connection attaches to the
live agent session when one exists, else opens its own (deleted on
disconnect). Human input pauses the agent via the agent_control API,
so the drawer needs no takeover overlay.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
from pathlib import Path
import os
import threading
import time
import urllib.request

GRID_BASE = "http://browser:4444"
VIEWPORT = {"width": 1280, "height": 800}
MOBILE_VIEWPORT = {"width": 390, "height": 844}


def is_phone_user_agent(user_agent):
    """True when a viewer User-Agent looks like a phone (or phone-
    sized tablet): the Mobi token covers mobile Chrome/Safari/Firefox,
    the rest catch platform UAs that omit it. Known gap (accepted):
    iPads in desktop mode send a Macintosh UA with no iPad token and
    are indistinguishable from a Mac — they get the desktop view."""
    ua = user_agent or ""
    return any(token in ua for token in
               ("Mobi", "Android", "iPhone", "iPad"))


def viewport_for(kind, user_agent):
    """(viewport, mobile) for a viewer session. Phone viewers with
    their OWN session get a 390x844 mobile viewport with touch
    emulation; shared/agent sessions keep the desktop viewport so
    every tab sees the same page. Pure (no I/O): unit-testable."""
    if kind == "own" and is_phone_user_agent(user_agent):
        return dict(MOBILE_VIEWPORT), True
    return dict(VIEWPORT), False
# Requested device pixels per CSS pixel. This MUST be 1: the
# viewer maps pointer coords as frame_px / advertised_dsf, which
# only lands when frames equal CSS px in every world — override
# applied (1280x800 view, DSF 1), override ignored (default 800x600
# view, DSF 1). DSF=2 was tried and measured broken live: frames
# arrived 1280px while the client divided by 2, so every click
# landed at half coords (quadrant probe HIT_B=Q1). Crispness via
# device pixels is sacrificed for a mapping that cannot be wrong;
# the bridge still measures window.devicePixelRatio per attach and
# advertises THAT (plus sizes the screencast from it) as backstop.
DSF = 1

# Start page for fresh viewer sessions (also the URL-box default).
# A new tab never repaints, so without an initial navigation the
# viewer sits on a black canvas until the human types a URL. Start
# on a neutral page, not the console itself (issue #1023:
# Buttons-inside-Buttons as first paint is disorienting).
DEFAULT_URL = "https://example.com"

VIEWER_HTML = (
    Path(__file__).with_name("browser_viewer.html").read_text(encoding="utf-8")
)

def _grid_session_create():
    # --disable-dev-shm-usage complements the 2gb shm_size: when raster
    # buffers spike, Chromium falls back to /tmp instead of SIGBUS/SIGKILL
    # on /dev/shm exhaustion (the #1 silent renderer crash in Docker).
    caps = {
        "browserName": "chrome",
        "goog:chromeOptions": {
            "args": [
                "--user-data-dir=/data/shared-browser/agent",
                "--disable-dev-shm-usage",
                "--enable-features=WebMCP",
            ]
        },
    }
    req = urllib.request.Request(
        GRID_BASE + "/session",
        data=json.dumps({"capabilities": {"alwaysMatch": caps}}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)["value"]["sessionId"]


def _grid_session_delete(session_id):
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                GRID_BASE + f"/session/{session_id}", method="DELETE"
            ),
            timeout=15,
        )
    except Exception:
        pass


#: Sessions carrying this capability were opened by an agent
#: driver (boomtick browser_cdp). Viewer/human sessions lack it —
#: attaching to one would mirror the viewer recursively.
AGENT_SESSION_MARKER = "boomtick:agentSession"


#: Shared viewer session (issue #1026). With a single Grid slot,
#: every viewer tab minting its own session wedges all but the
#: first — and each shows a different browser. The first viewer
#: tab creates the session and records it here; later tabs attach
#: to it while it is alive, so all tabs show the same page. Agent
#: sessions still take precedence for new viewers. Refcounted: the
#: last viewer disconnect deletes it (no warm-TTL policy to tune).
#: Single-worker uvicorn owns this registry (Dockerfile CMD sets
#: no --workers); a restart orphans the Grid session to its idle
#: timeout, and the next viewer transparently creates a new one.
_viewer_lock = threading.Lock()
_viewer_session_id: "str | None" = None
_viewer_refcount = 0
#: Canonical viewport for the shared session: the owner's
#: (viewport, mobile). Joining tabs adopt it so a desktop tab
#: joining a phone-owned session does not re-emulate the shared
#: Grid session out from under the phone (and vice versa) —
#: last-writer-wins metrics flap the screencast size for all.
_viewer_viewport: "dict | None" = None


def _grid_session_alive(session_id):
    """True when Grid still has the session (cheap read-only probe)."""
    try:
        with urllib.request.urlopen(
            GRID_BASE + f"/session/{session_id}/url", timeout=10
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


def _viewer_claim():
    """Attach to the live shared viewer session, if any.

    Returns the session id (refcounted) or None. Stale records are
    cleared so the next claim creates fresh.
    """
    global _viewer_session_id, _viewer_refcount, _viewer_viewport
    with _viewer_lock:
        sid = _viewer_session_id
        if sid is None:
            return None
        # Validate outside the lock: network I/O under a mutex
        # would serialize every viewer connect behind Grid latency.
        alive = _grid_session_alive(sid)
    with _viewer_lock:
        if not alive or _viewer_session_id != sid:
            if _viewer_session_id == sid:
                _viewer_session_id = None
                _viewer_refcount = 0
                _viewer_viewport = None
            return None
        _viewer_refcount += 1
        return sid


def _viewer_create(session_id, viewport=None, mobile=False):
    """Record a fresh viewer-owned session (refcount starts at 1).

    The owner's viewport is canonical for the session lifetime;
    joiners adopt it (see bridge construction) instead of
    re-emulating with their own.
    """
    global _viewer_session_id, _viewer_refcount, _viewer_viewport
    with _viewer_lock:
        _viewer_session_id = session_id
        _viewer_refcount = 1
        _viewer_viewport = {"viewport": dict(viewport)
                           if viewport is not None
                           else dict(VIEWPORT),
                           "mobile": bool(mobile)}


def _viewer_viewport_for(session_id):
    """Canonical (viewport, mobile) for a shared session, or None.

    None when the record is stale (owner predates this worker, or
    the session id does not match): callers fall back to computing
    their own via viewport_for.
    """
    with _viewer_lock:
        if _viewer_session_id != session_id or _viewer_viewport is None:
            return None
        return (dict(_viewer_viewport["viewport"]),
                _viewer_viewport["mobile"])


def _viewer_release(session_id):
    """Drop one viewer reference; True when the session must be
    deleted (last viewer out and the record is still ours)."""
    global _viewer_session_id, _viewer_refcount, _viewer_viewport
    with _viewer_lock:
        if _viewer_session_id != session_id:
            return False
        _viewer_refcount -= 1
        if _viewer_refcount <= 0:
            _viewer_session_id = None
            _viewer_refcount = 0
            _viewer_viewport = None
            return True
        return False


async def _viewer_teardown(session_id, kind):
    """Refcounted teardown for viewer-owned sessions (issue #1026):
    the last viewer out deletes the Grid session; agent attaches
    own nothing. Split out so the policy is unit-testable — the
    websocket finally only calls it."""
    import asyncio as aio

    if kind not in ("own", "viewer-shared"):
        return False
    last_out = await aio.to_thread(_viewer_release, session_id)
    if last_out:
        await aio.to_thread(_grid_session_delete, session_id)
    return last_out


def _grid_live_session():
    """Return an existing *agent* Grid session id, if any."""
    try:
        with urllib.request.urlopen(GRID_BASE + "/status", timeout=10) as resp:
            value = json.load(resp)["value"]
    except Exception:
        return None
    for node in value.get("nodes", []):
        for slot in node.get("slots", []):
            session = slot.get("session") or {}
            if not session.get("sessionId"):
                continue
            caps = session.get("capabilities") or {}
            if caps.get(AGENT_SESSION_MARKER):
                return session["sessionId"]
    return None


AGENT_API = os.environ.get("AGENT_API", "http://127.0.0.1:8000/api/agent")


def _pause_agent():
    try:
        req = urllib.request.Request(
            AGENT_API.rstrip("/") + "/pause",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
        return True
    except Exception:
        return False


KEYCODES = {
    "Enter": 13,
    "Backspace": 8,
    "Tab": 9,
    "Escape": 27,
    "Delete": 46,
    "ArrowLeft": 37,
    "ArrowUp": 38,
    "ArrowRight": 39,
    "ArrowDown": 40,
    "Home": 36,
    "End": 35,
}


class CDPBridge:
    """Minimal CDP client over the per-session Grid endpoint.

    Screencast policy: every frame is acknowledged to Chromium
    immediately on receipt (the compositor blocks until
    Page.screencastFrameAck arrives), and only the latest frame is
    flushed to the UI websocket — intermediate frames are dropped
    while a send is in flight so a laggy viewer cannot wedge the
    renderer or balloon server memory.
    """

    def __init__(self, session_id, viewport=None, mobile=False):
        self.session_id = session_id
        # The emulated viewport travels with the bridge: reattach
        # metrics and screencast sizing both follow it, so a phone
        # viewer stays 390x844 across target swaps and restarts.
        self.viewport = dict(viewport) if viewport is not None \
            else dict(VIEWPORT)
        self.mobile = mobile
        self._id = 0
        # Fire-and-forget ack ids live far above call ids so an ack
        # response can never collide with a tracked call future.
        self._ack_id = 10**9
        self._pending = {}
        self.ws = None
        self._latest_params = None
        self._dispatch_in_flight = False
        # Set by the app: reattach + restart the screencast after a
        # renderer crash instead of letting the socket die.
        self._crash_cb = None
        # Advertised device-pixel ratio, measured per attach in
        # reattach(); DSF until the first measure lands.
        self.dsf = DSF

    def _ws_url(self):
        return f"ws://browser:4444/session/{self.session_id}/se/cdp"

    async def connect(self):
        import websockets

        self.ws = await websockets.connect(self._ws_url(), max_size=8 << 20)
        asyncio.get_event_loop().create_task(self._reader())
        await self.reattach()

    async def reattach(self):
        # The Grid endpoint fronts the browser target: attach to the
        # page target (flattened) before any Page.* command resolves.
        # Re-run after every navigation: a cross-origin load can swap
        # the page target, leaving the old session (and its screencast)
        # pointed at a dead target with zero frames and dead input.
        targets = await self.call("Target.getTargets")
        pages = [t for t in targets["targetInfos"] if t["type"] == "page"]
        if not pages:
            # Crash/nav gap with no page target yet: raw StopIteration
            # would surface as an inscrutable 500-class failure.
            raise RuntimeError("no page target to attach")
        page = pages[0]
        attached = await self.call(
            "Target.attachToTarget", {"targetId": page["targetId"], "flatten": True}
        )
        self.cdp_session = attached["sessionId"]
        # Crisp text: render at device pixels, input stays in CSS px.
        await self.call("Emulation.setDeviceMetricsOverride",
                        {"width": self.viewport["width"],
                         "height": self.viewport["height"],
                         "deviceScaleFactor": DSF,
                         "mobile": self.mobile})
        if self.mobile:
            # Phone-owned sessions get touch input: without this,
            # taps land as mouse events and mobile pages misbehave.
            # Best-effort: a Grid that rejects it must not fail the
            # attach the desktop path just completed.
            with contextlib.suppress(Exception):
                await self.call("Emulation.setTouchEmulationEnabled",
                                {"enabled": True})        # Trust but verify: the override may not apply (headless
        # quirks, swapped targets). Advertise the measured ratio so
        # clicks never land at half coords again; on any error keep
        # the previous value (DSF until the first measure lands).
        try:
            dpr = await self.call(
                "Runtime.evaluate",
                {"expression": "window.devicePixelRatio", "returnByValue": True},
            )
            measured = (dpr or {}).get("result", {}).get("value")
            if isinstance(measured, (int, float)) and measured > 0:
                self.dsf = float(measured)
        except Exception:
            pass

    async def _reader(self):
        from websockets.exceptions import ConnectionClosed

        try:
            async for raw in self.ws:
                self.on_message(json.loads(raw))
        except ConnectionClosed:
            pass

    def on_message(self, msg):
        # Unwrap flattened target messages to the inner page event.
        if msg.get("method") == "Target.receivedMessageFromTarget":
            try:
                msg = json.loads(msg["params"]["message"])
            except Exception:
                return
        method = msg.get("method", "")
        if method == "Page.screencastFrame":
            if getattr(self, "_frame_cb", None) is not None:
                try:
                    loop = asyncio.get_event_loop()
                except Exception:
                    return
                loop.create_task(self._ack_and_dispatch(msg.get("params", {})))
            return
        if "targetCrashed" in method:
            cb = getattr(self, "_crash_cb", None)
            if cb is not None:

                async def _run():
                    try:
                        res = cb()
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception:
                        pass

                try:
                    asyncio.get_event_loop().create_task(_run())
                except Exception:
                    pass
            return
        if "id" in msg and msg["id"] in self._pending:
            fut = self._pending.pop(msg["id"])
            if not fut.done():
                fut.set_result(msg.get("result", {}))

    async def _ack_and_dispatch(self, params):
        # Ack first so the compositor never blocks; then coalesce —
        # only the latest frame is flushed while a send is in flight.
        try:
            await self.ack_frame(params.get("sessionId"))
        except Exception:
            pass
        self._latest_params = params
        if self._dispatch_in_flight:
            return
        self._dispatch_in_flight = True
        try:
            while self._latest_params is not None:
                current = self._latest_params
                self._latest_params = None
                try:
                    await self._frame_cb(current)
                except Exception:
                    return
        finally:
            self._dispatch_in_flight = False

    async def call(self, method, params=None, timeout=15):
        import asyncio as aio

        self._id += 1
        fut = aio.get_event_loop().create_future()
        self._pending[self._id] = fut
        envelope = {"id": self._id, "method": method, "params": params or {}}
        if getattr(self, "cdp_session", None):
            envelope["sessionId"] = self.cdp_session
        await self.ws.send(json.dumps(envelope))
        return await aio.wait_for(fut, timeout)

    async def start_screencast(self, on_frame):
        self._frame_cb = on_frame
        self._latest_params = None
        self._dispatch_in_flight = False
        await self.call("Page.enable")
        try:
            await self.call("Page.stopScreencast")
        except Exception:
            pass
        # Size the capture from the MEASURED ratio, not the request:
        # clamping below device px would reintroduce a scale the
        # viewer does not know about.
        scale = self.dsf if isinstance(self.dsf, (int, float)) \
            and self.dsf > 0 else 1
        await self.call("Page.startScreencast",
                        {"format": "jpeg", "quality": 70,
                         "maxWidth": int(self.viewport["width"] * scale),
                         "maxHeight": int(self.viewport["height"] * scale),
                         "everyNthFrame": 1})
    async def ack_frame(self, session_id):
        self._ack_id += 1
        envelope = {
            "id": self._ack_id,
            "method": "Page.screencastFrameAck",
            "params": {"sessionId": session_id},
        }
        if getattr(self, "cdp_session", None):
            envelope["sessionId"] = self.cdp_session
        await self.ws.send(json.dumps(envelope))

    async def click(self, x, y):
        base = {"x": x, "y": y, "button": "left", "clickCount": 1}
        await self.call("Input.dispatchMouseEvent", {**base, "type": "mousePressed"})
        await self.call("Input.dispatchMouseEvent", {**base, "type": "mouseReleased"})

    async def move(self, x, y):
        # Hover-only: no buttons, no agent pause. Lets the remote
        # page fire :hover and repaint under the cursor.
        await self.call("Input.dispatchMouseEvent",
                        {"x": x, "y": y, "type": "mouseMoved"})

    async def wheel(self, x, y, dx, dy):
        # Scroll the remote page, not the viewer chrome. No agent
        # pause: scrolling is look-around input, like a hover.
        await self.call("Input.dispatchMouseEvent",
                        {"x": x, "y": y, "type": "mouseWheel",
                         "deltaX": dx, "deltaY": dy})

    async def touch(self, phase, x, y):
        # One-finger drag-scroll. touchEnd carries no points: the
        # finger is gone by definition.
        if phase == "end":
            await self.call("Input.dispatchTouchEvent",
                            {"type": "touchEnd", "touchPoints": []})
            return
        await self.call("Input.dispatchTouchEvent",
                        {"type": "touchStart" if phase == "start"
                         else "touchMove",
                         "touchPoints": [{"x": x, "y": y}]})

    async def ensure_focus(self):
        # Keystrokes drop when nothing is focused: focus the active
        # element, else the first field, else the body.
        await self.call(
            "Runtime.evaluate",
            {
                "expression": (
                    "(()=>{const a=document.activeElement;"
                    "if(a&&a!==document.body)return a.tagName;"
                    "(document.querySelector("
                    "'input,textarea,[contenteditable=\"true\"]')"
                    "||document.body).focus();"
                    "return document.activeElement.tagName;})()"
                )
            },
        )

    async def key(self, key, code):
        try:
            await self.ensure_focus()
        except Exception:
            # Focus is best-effort: a failed focus probe must not
            # eat the keystroke when something is already focused.
            pass
        if len(key) == 1:
            await self.call("Input.insertText", {"text": key})
            return
        vk = KEYCODES.get(key)
        if vk is None:
            raise ValueError(f"unknown key: {key!r}")
        for typ in ("keyDown", "keyUp"):
            params = {
                "type": typ,
                "key": key,
                "windowsVirtualKeyCode": vk,
                "code": code or key,
            }
            if typ == "keyDown":
                params["text"] = "\r" if key == "Enter" else ""
            await self.call("Input.dispatchKeyEvent", params)

    async def type_text(self, text):
        # Bulk commit from the on-screen keyboard: text that never
        # existed as key presses takes the protocol-blessed IME path.
        # Focus first, same best-effort deal as key(): a fresh tab
        # has nothing focused, and insertText needs a target.
        with contextlib.suppress(Exception):
            await self.ensure_focus()
        await self.call("Input.insertText", {"text": text})

    async def navigate(self, url):
        await self.call("Page.navigate", {"url": url})

    async def close(self):
        try:
            await self.ws.close()
        except Exception:
            pass


async def _ws_send_text(send, lock, text):
    async with lock:
        await send({"type": "websocket.send", "text": text})


def create_app():
    """Raw ASGI app (no framework router: the baked FastAPI/Starlette
    combo in terminal images rejects websocket routes at the
    handshake; raw ASGI speaks directly to uvicorn)."""

    async def app(scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] == "http":
            body = VIEWER_HTML.replace("__DEFAULT_URL__", DEFAULT_URL).encode()
            # No-store: the viewer ships new JS with deploys and a
            # cached copy silently pins old behavior (e.g. a dead URL
            # box) with zero errors to diagnose.
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type",
                                     b"text/html; charset=utf-8"),
                                    (b"cache-control", b"no-store")]})
            await send({"type": "http.response.body", "body": body})
            return
        if scope["type"] != "websocket" or not path.endswith("/ws"):
            await send({"type": "websocket.close", "code": 4404})
            return
        await send({"type": "websocket.accept"})
        # Phone detection for viewport choice: read the viewer UA
        # from the handshake headers (kind=='own' + phone => 390x844
        # mobile emulation; shared/agent sessions stay desktop so
        # every tab sees the same page).
        user_agent = ""
        for _name, _value in scope.get("headers", []):
            # errors="ignore": a malformed header must not break the
            # handshake (and decode then cannot raise, so no handler).
            if _name.decode("latin-1", "ignore").lower() \
                    == "user-agent":
                user_agent = _value.decode("latin-1", "ignore")
                break
        # Shared autonomy: attach to the live agent session when one
        # exists; otherwise open our own (deleted on disconnect).
        # A full Grid (single slot) or a down Grid raises here: tell
        # the viewer instead of dying before 'attached' (the page
        # would hang on "connecting…" with an ASGI traceback).
        try:
            session_id = await asyncio.to_thread(_grid_live_session)
            if session_id is not None:
                kind = "agent"
            else:
                claimed = await asyncio.to_thread(_viewer_claim)
                if claimed is not None:
                    session_id = claimed
                    kind = "viewer-shared"
                else:
                    try:
                        session_id = await asyncio.to_thread(_grid_session_create)
                    except Exception:
                        # Lost the create race: another tab just
                        # stored the shared session — attach to it
                        # instead of reporting busy for a slot we
                        # now share.
                        claimed = await asyncio.to_thread(_viewer_claim)
                        if claimed is None:
                            raise
                        session_id = claimed
                        kind = "viewer-shared"
                    else:
                        kind = "own"
                        # Record the owner's viewport as canonical:
                        # joiners adopt it below so heterogeneous
                        # tabs never re-emulate the shared session.
                        _viewer_create(
                            session_id,
                            *viewport_for(kind, user_agent))
        except Exception:
            try:
                await _ws_send_text(
                    send,
                    asyncio.Lock(),
                    json.dumps(
                        {
                            "type": "error",
                            "msg": ("browser busy — no free Grid slot; retry shortly"),
                        }
                    ),
                )
            except Exception:
                pass
            await send({"type": "websocket.close", "code": 1013})
            return
        await _ws_send_text(
            send,
            asyncio.Lock(),
            json.dumps(
                {
                    "type": "attached",
                    "kind": kind,
                    "session": session_id[:8],
                    "dsf": DSF,
                }
            ),
        )
        bridge = None
        paused = False
        lock = asyncio.Lock()

        async def on_frame(params):
            # Ack-first + latest-wins live in the bridge: this only
            # flushes the coalesced frame to the viewer.
            await _ws_send_text(
                send, lock, json.dumps({"type": "frame", "data": params["data"]})
            )

        async def recover_once(op):
            # One reattach + screencast restart, then retry: a swapped
            # or crashed target must not kill the viewer socket.
            # Deterministic input rejections (ValueError: unknown key,
            # no page target) skip the retry and report immediately.
            async def report(msg):
                try:
                    await _ws_send_text(
                        send, lock, json.dumps({"type": "error", "msg": msg})
                    )
                except Exception:
                    pass

            try:
                await op()
                return
            except ValueError as exc:
                await report(f"browser op rejected: {exc}")
                return
            except Exception:
                pass
            try:
                await bridge.reattach()
                await bridge.start_screencast(on_frame)
                await op()
            except ValueError as exc:
                await report(f"browser op rejected: {exc}")
            except Exception:
                await report("browser op failed")

        async def on_crash():
            try:
                await bridge.reattach()
                await bridge.start_screencast(on_frame)
            except Exception:
                return
            try:
                await _ws_send_text(
                    send, lock, json.dumps({"type": "dsf", "dsf": bridge.dsf})
                )
                await _ws_send_text(
                    send,
                    lock,
                    json.dumps(
                        {"type": "reattached", "msg": "reattached after target crash"}
                    ),
                )
            except Exception:
                pass

        try:
            viewport, mobile = viewport_for(kind, user_agent)
            if kind == "viewer-shared":
                # Adopt the owner's canonical viewport: a desktop
                # tab joining a phone-owned session must not
                # re-emulate the shared Grid session to 1280px
                # (and vice versa). None when the owner predates
                # this worker — then compute our own as before.
                adopted = _viewer_viewport_for(session_id)
                if adopted is not None:
                    viewport, mobile = adopted
            bridge = CDPBridge(session_id, viewport=viewport,
                               mobile=mobile)
            try:
                await bridge.connect()
            except Exception as exc:
                # Past 'attached' with no bridge: without this the
                # viewer gets frames-never plus silence. Name it and
                # close so the viewer re-dials instead of hanging.
                try:
                    await _ws_send_text(
                        send,
                        lock,
                        json.dumps(
                            {"type": "error", "msg": f"browser connect failed: {exc}"}
                        ),
                    )
                except Exception:
                    pass
                await send({"type": "websocket.close", "code": 1011})
                return
            bridge._crash_cb = on_crash
            # Measured ratio beats the assumed DSF: the viewer maps
            # pointer coords with this, before the first frame lands.
            await _ws_send_text(
                send, lock, json.dumps({"type": "dsf", "dsf": bridge.dsf})
            )
            await bridge.start_screencast(on_frame)

            async def do_go(url):
                await bridge.navigate(url)
                # Let the load commit, then bind the (possibly
                # swapped) page target and restart the screencast
                # on it — otherwise the viewer goes dark.
                await asyncio.sleep(1.5)
                await bridge.reattach()
                # Reattach re-measures: push the fresh divisor or
                # post-nav clicks use the stale one and mis-land.
                await _ws_send_text(
                    send, lock, json.dumps({"type": "dsf", "dsf": bridge.dsf})
                )
                await bridge.start_screencast(on_frame)

            if kind == "own":
                # A fresh tab never repaints: without this the new
                # viewer sits on a black canvas (fps: -) until the
                # human types a URL. Start on the neutral page.
                # Shared/agent attaches skip this: re-navigating
                # would yank the other tabs' page out from under them.
                await recover_once(lambda: do_go(DEFAULT_URL))
            while True:
                event = await receive()
                if event["type"] == "websocket.disconnect":
                    break
                if event["type"] != "websocket.receive":
                    continue
                try:
                    msg = json.loads(event.get("text") or "{}")
                except Exception:
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("t") == "click":
                    await human_input()
                    try:
                        x, y = int(msg["x"]), int(msg["y"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    await recover_once(lambda: bridge.click(x, y))
                elif msg.get("t") == "move":
                    try:
                        x, y = int(msg["x"]), int(msg["y"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    await recover_once(lambda: bridge.move(x, y))
                elif msg.get("t") == "wheel":
                    try:
                        x, y = int(msg["x"]), int(msg["y"])
                        dx, dy = int(msg.get("dx", 0)), int(msg.get("dy", 0))
                    except (KeyError, TypeError, ValueError):
                        continue
                    await recover_once(lambda: bridge.wheel(x, y, dx, dy))
                elif msg.get("t") == "touch":
                    phase = str(msg.get("phase", ""))
                    if phase not in ("start", "move", "end"):
                        continue
                    try:
                        x, y = int(msg["x"]), int(msg["y"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    await recover_once(lambda: bridge.touch(phase, x, y))
                elif msg.get("t") == "key":
                    await human_input()
                    key, code = (str(msg.get("key", "")), str(msg.get("code", "")))
                    await recover_once(lambda: bridge.key(key, code))
                elif msg.get("t") == "type":
                    await human_input()
                    text = str(msg.get("text", ""))
                    if not text:
                        continue
                    await recover_once(
                        lambda: bridge.type_text(text))
                elif msg.get("t") == "go":
                    await human_input()
                    url = str(msg.get("url") or "")
                    if not url:
                        continue
                    await recover_once(lambda: do_go(url))
        finally:
            if bridge is not None:
                await bridge.close()
            await _viewer_teardown(session_id, kind)

    return app


async def self_test():
    """Headless proof: frames flow, click + typing land, cleanup."""
    session_id = await asyncio.to_thread(_grid_session_create)
    got = []
    typed = None
    try:
        bridge = CDPBridge(session_id)
        await bridge.connect()

        async def on_frame(params):
            # The bridge acks each frame on receipt; this only records.
            got.append((time.monotonic(), len(params["data"])))

        await bridge.start_screencast(on_frame)
        await bridge.navigate("http://caddy:8080/buttons/")
        await asyncio.sleep(3)
        # Screencast is repaint-driven: force continuous visual change.
        await bridge.call(
            "Runtime.evaluate",
            {
                "expression": "setInterval(() => {document.body.style."
                "background = '#' + Math.floor(Math.random()*"
                "16777215).toString(16);}, 100);"
            },
        )
        await asyncio.sleep(4)
        await bridge.click(640, 200)
        await asyncio.sleep(3)
        # Typing round-trip on a real input (focus guaranteed by key()).
        await bridge.navigate("data:text/html,<input id=t value=''>")
        await asyncio.sleep(1)
        for ch in "hi42":
            await bridge.key(ch, "Key" + ch.upper())
        typed = await bridge.call(
            "Runtime.evaluate", {"expression": "document.getElementById('t').value"}
        )
        await bridge.close()
    finally:
        await asyncio.to_thread(_grid_session_delete, session_id)
    n = len(got)
    span = (got[-1][0] - got[0][0]) if n > 1 else 0.0
    fps = n / span if span > 0 else 0.0
    sizes = sorted(s for _, s in got)
    typed_value = (typed or {}).get("result", {}).get("value")
    print(
        f"frames={n} span_s={span:.1f} fps={fps:.1f} "
        f"median_bytes={sizes[len(sizes) // 2] if sizes else 0} "
        f"typed={typed_value!r}"
    )
    print("OK" if (n >= 10 and fps >= 5 and typed_value == "hi42") else "WEAK")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        asyncio.run(self_test())
        return
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
