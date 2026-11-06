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
import base64
import json
import os
import time
import urllib.request

GRID_BASE = "http://browser:4444"
VIEWPORT = {"width": 1280, "height": 800}
# Device pixels per CSS pixel. Screencast frames arrive at
# width*DSF; the viewer divides pointer coords by DSF so CDP input
# (CSS viewport space) lands where the human clicked.
DSF = 2

# Start page for fresh viewer sessions (also the URL-box default).
# A new tab never repaints, so without an initial navigation the
# viewer sits on a black canvas until the human types a URL.
DEFAULT_URL = "http://caddy:8080/buttons/"

VIEWER_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>CDP direct viewer (spike)</title>
<style>
body{margin:0;background:#111;color:#eee;font-family:sans-serif}
#bar{display:flex;gap:8px;padding:8px;align-items:center}
#nav{display:flex;gap:8px;flex:1}
#url{flex:1;padding:6px}
#stats{padding:0 8px 8px;font-size:13px;color:#9f9}
#screen{display:block;max-width:100vw;border-top:1px solid #333;cursor:default}
</style></head><body>
<div id="bar"><form id="nav"><input id="url" value="__DEFAULT_URL__" aria-label="Address"><button id="go" type="submit">Go</button></form><span id="state">connecting…</span></div>
<div id="stats">fps: - | last input→frame: - ms</div>
<canvas id="screen"></canvas>
<script>
const base = location.pathname.replace(/\/$/, '');
const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + base + '/ws');
const cv = document.getElementById('screen'), ctx = cv.getContext('2d');
cv.tabIndex = 0;
const img = new Image(); let frames = 0, t0 = performance.now(), lastClick = 0;
let dsf = 1;
ws.onopen = () => document.getElementById('state').textContent = 'live';
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.type === 'attached') {
    dsf = m.dsf || 1;
    document.getElementById('state').textContent =
      (m.shared ? 'shared agent session ' : 'own session ') + m.session;
    return;
  }
  if (m.type === 'status' || m.type === 'reattached') {
    document.getElementById('state').textContent = m.msg || m.type;
    return;
  }
  if (m.type === 'error') {
    document.getElementById('state').textContent = 'error: ' + (m.msg || 'browser op failed');
    return;
  }
  if (m.type === 'frame') {
    img.onload = () => {
      if (cv.width !== img.width) { cv.width = img.width; cv.height = img.height; }
      ctx.drawImage(img, 0, 0); frames++;
      const el = (performance.now() - t0) / 1000;
      if (el >= 2) {
        document.querySelector('#stats').textContent =
          'fps: ' + (frames / el).toFixed(1) +
          ' | last input→frame: ' + (lastClick ? (performance.now() - lastClick).toFixed(0) + ' ms' : '-');
      }
      URL.revokeObjectURL(img.src);
    };
    img.src = URL.createObjectURL(new Blob(
      [Uint8Array.from(atob(m.data), c => c.charCodeAt(0))], {type: 'image/jpeg'}));
  }
};
function pos(e) {
  const r = cv.getBoundingClientRect();
  // Canvas backing store is device pixels; CDP input is CSS pixels.
  return {x: Math.round((e.clientX - r.left) * cv.width / r.width / dsf),
          y: Math.round((e.clientY - r.top) * cv.height / r.height / dsf)};
}
cv.addEventListener('click', (e) => { cv.focus(); lastClick = performance.now(); ws.send(JSON.stringify({t: 'click', ...pos(e)})); });
cv.addEventListener('keydown', (e) => {
  e.preventDefault(); lastClick = performance.now();
  ws.send(JSON.stringify({t: 'key', key: e.key, code: e.code}));
});
function normalizeUrl(raw) {
  // Chrome-omnibox-lite: trim first; keep explicit schemes and
  // chrome pages; bare hostnames ride https://; bare host:port
  // rides http:// (alt ports are near-always plain-http local
  // services); spaced input is left for the engine to reject
  // visibly rather than mangled here.
  const v = (raw || '').trim();
  if (!v || /\s/.test(v)) return v;
  if (/^https?:\/\//i.test(v)) return v;
  if (/^(about|data|blob):/i.test(v)) return v;
  if (/^[^/:?\s]+:\d+(\/|$|\?)/.test(v)) return 'http://' + v;
  return 'https://' + v;
}
function go() {
  const box = document.getElementById('url');
  const url = normalizeUrl(box.value);
  if (!url) return;
  box.value = url;
  ws.send(JSON.stringify({t: 'go', url}));
}
// Form submit (not button click) so Enter in the address box and
// the Go button take the same path — keyboard and touch alike.
document.getElementById('nav').onsubmit = (e) => { e.preventDefault(); go(); };
</script></body></html>
"""


def _grid_session_create():
    # --disable-dev-shm-usage complements the 2gb shm_size: when raster
    # buffers spike, Chromium falls back to /tmp instead of SIGBUS/SIGKILL
    # on /dev/shm exhaustion (the #1 silent renderer crash in Docker).
    caps = {"browserName": "chrome",
            "goog:chromeOptions": {"args": [
                "--user-data-dir=/data/shared-browser/agent",
                "--disable-dev-shm-usage",
                "--enable-features=WebMCP"]}}
    req = urllib.request.Request(
        GRID_BASE + "/session",
        data=json.dumps({"capabilities": {"alwaysMatch": caps}}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)["value"]["sessionId"]


def _grid_session_delete(session_id):
    try:
        urllib.request.urlopen(urllib.request.Request(
            GRID_BASE + f"/session/{session_id}", method="DELETE"),
            timeout=15)
    except Exception:
        pass


#: Sessions carrying this capability were opened by an agent
#: driver (boomtick browser_cdp). Viewer/human sessions lack it —
#: attaching to one would mirror the viewer recursively.
AGENT_SESSION_MARKER = "boomtick:agentSession"


def _grid_live_session():
    """Return an existing *agent* Grid session id, if any."""
    try:
        with urllib.request.urlopen(
                GRID_BASE + "/status", timeout=10) as resp:
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
            AGENT_API.rstrip("/") + "/pause", data=b"{}",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10):
            pass
        return True
    except Exception:
        return False


KEYCODES = {"Enter": 13, "Backspace": 8, "Tab": 9, "Escape": 27,
            "Delete": 46, "ArrowLeft": 37, "ArrowUp": 38,
            "ArrowRight": 39, "ArrowDown": 40, "Home": 36, "End": 35}


class CDPBridge:
    """Minimal CDP client over the per-session Grid endpoint.

    Screencast policy: every frame is acknowledged to Chromium
    immediately on receipt (the compositor blocks until
    Page.screencastFrameAck arrives), and only the latest frame is
    flushed to the UI websocket — intermediate frames are dropped
    while a send is in flight so a laggy viewer cannot wedge the
    renderer or balloon server memory.
    """

    def __init__(self, session_id):
        self.session_id = session_id
        self._id = 0
        # Fire-and-forget ack ids live far above call ids so an ack
        # response can never collide with a tracked call future.
        self._ack_id = 10 ** 9
        self._pending = {}
        self.ws = None
        self._latest_params = None
        self._dispatch_in_flight = False
        # Set by the app: reattach + restart the screencast after a
        # renderer crash instead of letting the socket die.
        self._crash_cb = None

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
        page = next(t for t in targets["targetInfos"]
                    if t["type"] == "page")
        attached = await self.call("Target.attachToTarget",
                                   {"targetId": page["targetId"],
                                    "flatten": True})
        self.cdp_session = attached["sessionId"]
        # Crisp text: render at device pixels, input stays in CSS px.
        await self.call("Emulation.setDeviceMetricsOverride",
                        {"width": VIEWPORT["width"],
                         "height": VIEWPORT["height"],
                         "deviceScaleFactor": DSF, "mobile": False})

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
                loop.create_task(
                    self._ack_and_dispatch(msg.get("params", {})))
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
        envelope = {"id": self._id, "method": method,
                    "params": params or {}}
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
        await self.call("Page.startScreencast",
                        {"format": "jpeg", "quality": 70,
                         "maxWidth": VIEWPORT["width"] * DSF,
                         "maxHeight": VIEWPORT["height"] * DSF,
                         "everyNthFrame": 1})

    async def ack_frame(self, session_id):
        self._ack_id += 1
        envelope = {"id": self._ack_id,
                    "method": "Page.screencastFrameAck",
                    "params": {"sessionId": session_id}}
        if getattr(self, "cdp_session", None):
            envelope["sessionId"] = self.cdp_session
        await self.ws.send(json.dumps(envelope))

    async def click(self, x, y):
        base = {"x": x, "y": y, "button": "left", "clickCount": 1}
        await self.call("Input.dispatchMouseEvent",
                       {**base, "type": "mousePressed"})
        await self.call("Input.dispatchMouseEvent",
                       {**base, "type": "mouseReleased"})

    async def ensure_focus(self):
        # Keystrokes drop when nothing is focused: focus the active
        # element, else the first field, else the body.
        await self.call("Runtime.evaluate", {"expression": (
            "(()=>{const a=document.activeElement;"
            "if(a&&a!==document.body)return a.tagName;"
            "(document.querySelector("
            "'input,textarea,[contenteditable=\"true\"]')"
            "||document.body).focus();"
            "return document.activeElement.tagName;})()")})

    async def key(self, key, code):
        await self.ensure_focus()
        if len(key) == 1:
            await self.call("Input.insertText", {"text": key})
            return
        vk = KEYCODES.get(key)
        if vk is None:
            return
        for typ in ("keyDown", "keyUp"):
            params = {"type": typ, "key": key,
                      "windowsVirtualKeyCode": vk, "code": code or key}
            if typ == "keyDown":
                params["text"] = "\r" if key == "Enter" else ""
            await self.call("Input.dispatchKeyEvent", params)

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
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type",
                                     b"text/html; charset=utf-8")]})
            await send({"type": "http.response.body", "body": body})
            return
        if scope["type"] != "websocket" or not path.endswith("/ws"):
            await send({"type": "websocket.close", "code": 4404})
            return
        await send({"type": "websocket.accept"})
        # Shared autonomy: attach to the live agent session when one
        # exists; otherwise open our own (deleted on disconnect).
        session_id = await asyncio.to_thread(_grid_live_session)
        own_session = session_id is None
        if own_session:
            session_id = await asyncio.to_thread(_grid_session_create)
        await _ws_send_text(
            send, asyncio.Lock(),
            json.dumps({"type": "attached", "shared": not own_session,
                        "session": session_id[:8], "dsf": DSF}))
        bridge = None
        paused = False
        lock = asyncio.Lock()

        async def on_frame(params):
            # Ack-first + latest-wins live in the bridge: this only
            # flushes the coalesced frame to the viewer.
            await _ws_send_text(
                send, lock,
                json.dumps({"type": "frame", "data": params["data"]}))

        async def human_input():
            nonlocal paused
            if not paused:
                paused = await asyncio.to_thread(_pause_agent)

        async def recover_once(op):
            # One reattach + screencast restart, then retry: a swapped
            # or crashed target must not kill the viewer socket.
            try:
                await op()
            except Exception:
                try:
                    await bridge.reattach()
                    await bridge.start_screencast(on_frame)
                    await op()
                except Exception:
                    try:
                        await _ws_send_text(
                            send, lock,
                            json.dumps({"type": "error",
                                        "msg": "browser op failed"}))
                    except Exception:
                        pass

        async def on_crash():
            try:
                await bridge.reattach()
                await bridge.start_screencast(on_frame)
            except Exception:
                return
            try:
                await _ws_send_text(
                    send, lock,
                    json.dumps({"type": "reattached",
                                "msg": "reattached after target crash"}))
            except Exception:
                pass

        try:
            bridge = CDPBridge(session_id)
            await bridge.connect()
            bridge._crash_cb = on_crash
            await bridge.start_screencast(on_frame)

            async def do_go(url):
                await bridge.navigate(url)
                # Let the load commit, then bind the (possibly
                # swapped) page target and restart the screencast
                # on it — otherwise the viewer goes dark.
                await asyncio.sleep(1.5)
                await bridge.reattach()
                await bridge.start_screencast(on_frame)

            if own_session:
                # A fresh tab never repaints: without this the new
                # viewer sits on a black canvas (fps: -) until the
                # human types a URL. Start where the work is.
                await recover_once(lambda: do_go(DEFAULT_URL))
            while True:
                event = await receive()
                if event["type"] == "websocket.disconnect":
                    break
                if event["type"] != "websocket.receive":
                    continue
                msg = json.loads(event.get("text") or "{}")
                if msg.get("t") == "click":
                    await human_input()
                    x, y = int(msg["x"]), int(msg["y"])
                    await recover_once(lambda: bridge.click(x, y))
                elif msg.get("t") == "key":
                    await human_input()
                    key, code = (str(msg.get("key", "")),
                                 str(msg.get("code", "")))
                    await recover_once(lambda: bridge.key(key, code))
                elif msg.get("t") == "go":
                    url = str(msg["url"])
                    await recover_once(lambda: do_go(url))
        finally:
            if bridge is not None:
                await bridge.close()
            if own_session:
                await asyncio.to_thread(_grid_session_delete, session_id)

    return app


async def self_test():
    """Headless proof: frames flow, click + typing land, cleanup."""
    session_id = await asyncio.to_thread(_grid_session_create)
    got = []
    typed = None
    try:
        bridge = CDPBridge(session_id)
        await bridge.connect()
        t_click = None

        async def on_frame(params):
            # The bridge acks each frame on receipt; this only records.
            got.append((time.monotonic(), len(params["data"])))

        await bridge.start_screencast(on_frame)
        await bridge.navigate("http://caddy:8080/buttons/")
        await asyncio.sleep(3)
        # Screencast is repaint-driven: force continuous visual change.
        await bridge.call(
            "Runtime.evaluate",
            {"expression": "setInterval(() => {document.body.style."
                           "background = '#' + Math.floor(Math.random()*"
                           "16777215).toString(16);}, 100);"})
        await asyncio.sleep(4)
        t_click = time.monotonic()
        await bridge.click(640, 200)
        await asyncio.sleep(3)
        # Typing round-trip on a real input (focus guaranteed by key()).
        await bridge.navigate("data:text/html,<input id=t value=''>")
        await asyncio.sleep(1)
        for ch in "hi42":
            await bridge.key(ch, "Key" + ch.upper())
        typed = await bridge.call(
            "Runtime.evaluate",
            {"expression": "document.getElementById('t').value"})
        await bridge.close()
    finally:
        await asyncio.to_thread(_grid_session_delete, session_id)
    n = len(got)
    span = (got[-1][0] - got[0][0]) if n > 1 else 0.0
    fps = n / span if span > 0 else 0.0
    sizes = sorted(s for _, s in got)
    typed_value = (typed or {}).get("result", {}).get("value")
    print(f"frames={n} span_s={span:.1f} fps={fps:.1f} "
          f"median_bytes={sizes[len(sizes)//2] if sizes else 0} "
          f"typed={typed_value!r}")
    print("OK" if (n >= 10 and fps >= 5
                   and typed_value == "hi42") else "WEAK")


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
