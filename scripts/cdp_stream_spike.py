"""CDP-direct shared-autonomy spike (read-only addition, no stack changes).

Streams the headed Grid browser to a lightweight canvas viewer over one
websocket and relays human input back as CDP Input events -- no X11/VNC
pixels in the loop. One viewer connection holds one Grid session (the
single slot); disconnecting always deletes it.

Run (terminal container)::

    python3 scripts/cdp_stream_spike.py --port 8099

Open http://<host>:8099/ in a browser.
Self-test (no browser needed)::

    python3 scripts/cdp_stream_spike.py --self-test
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
import urllib.request

GRID_BASE = "http://browser:4444"
VIEWPORT = {"width": 1280, "height": 800}

VIEWER_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>CDP direct viewer (spike)</title>
<style>
body{margin:0;background:#111;color:#eee;font-family:sans-serif}
#bar{display:flex;gap:8px;padding:8px;align-items:center}
#url{flex:1;padding:6px}
#stats{padding:0 8px 8px;font-size:13px;color:#9f9}
#screen{display:block;max-width:100vw;border-top:1px solid #333;cursor:crosshair}
</style></head><body>
<div id="bar"><input id="url" value="http://caddy:8080/buttons/">
<button id="go">Go</button><span id="state">connecting…</span></div>
<div id="stats">fps: - | last input→frame: - ms</div>
<canvas id="screen"></canvas>
<script>
const base = location.pathname.replace(/\/$/, '');
const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + base + '/ws');
const cv = document.getElementById('screen'), ctx = cv.getContext('2d');
const img = new Image(); let frames = 0, t0 = performance.now(), lastClick = 0;
ws.onopen = () => document.getElementById('state').textContent = 'live';
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
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
  return {x: Math.round((e.clientX - r.left) * cv.width / r.width),
          y: Math.round((e.clientY - r.top) * cv.height / r.height)};
}
cv.addEventListener('click', (e) => { lastClick = performance.now(); ws.send(JSON.stringify({t: 'click', ...pos(e)})); });
document.getElementById('go').onclick = () =>
  ws.send(JSON.stringify({t: 'go', url: document.getElementById('url').value}));
</script></body></html>
"""


def _grid_session_create():
    caps = {"browserName": "chrome",
            "goog:chromeOptions": {"args": [
                "--user-data-dir=/data/shared-browser/agent",
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


class CDPBridge:
    """Minimal CDP client over the per-session Grid endpoint."""

    def __init__(self, session_id):
        self.session_id = session_id
        self._id = 0
        self._pending = {}
        self.ws = None

    def _ws_url(self):
        return f"ws://browser:4444/session/{self.session_id}/se/cdp"

    async def connect(self):
        import websockets
        self.ws = await websockets.connect(self._ws_url(), max_size=8 << 20)
        asyncio.get_event_loop().create_task(self._reader())
        # The Grid endpoint fronts the browser target: attach to the
        # page target (flattened) before any Page.* command resolves.
        targets = await self.call("Target.getTargets")
        page = next(t for t in targets["targetInfos"]
                    if t["type"] == "page")
        attached = await self.call("Target.attachToTarget",
                                   {"targetId": page["targetId"],
                                    "flatten": True})
        self.cdp_session = attached["sessionId"]

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
            msg = json.loads(msg["params"]["message"])
        if (msg.get("method") == "Page.screencastFrame"
                and getattr(self, "_frame_cb", None)):
            asyncio.get_event_loop().create_task(
                self._frame_cb(msg.get("params", {})))
            return
        if "id" in msg and msg["id"] in self._pending:
            fut = self._pending.pop(msg["id"])
            if not fut.done():
                fut.set_result(msg.get("result", {}))

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
        await self.call("Page.enable")
        await self.call("Page.startScreencast",
                        {"format": "jpeg", "quality": 70,
                         "maxWidth": VIEWPORT["width"],
                         "maxHeight": VIEWPORT["height"],
                         "everyNthFrame": 1})

    async def ack_frame(self, session_id):
        envelope = {"id": 10 ** 9,
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
        if scope["type"] == "http":
            body = VIEWER_HTML.encode()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type",
                                     b"text/html; charset=utf-8")]})
            await send({"type": "http.response.body", "body": body})
            return
        if scope["type"] != "websocket" or scope.get("path") != "/ws":
            await send({"type": "websocket.close", "code": 4404})
            return
        await send({"type": "websocket.accept"})
        session_id = await asyncio.to_thread(_grid_session_create)
        bridge = CDPBridge(session_id)
        lock = asyncio.Lock()

        async def on_frame(params):
            await _ws_send_text(
                send, lock,
                json.dumps({"type": "frame", "data": params["data"]}))
            await bridge.ack_frame(params["sessionId"])

        try:
            await bridge.connect()
            await bridge.start_screencast(on_frame)
            while True:
                event = await receive()
                if event["type"] == "websocket.disconnect":
                    break
                if event["type"] != "websocket.receive":
                    continue
                msg = json.loads(event.get("text") or "{}")
                if msg.get("t") == "click":
                    await bridge.click(int(msg["x"]), int(msg["y"]))
                elif msg.get("t") == "go":
                    await bridge.navigate(str(msg["url"]))
        finally:
            await bridge.close()
            await asyncio.to_thread(_grid_session_delete, session_id)

    return app


async def self_test():
    """Headless proof: frames flow, click dispatches, session deleted."""
    session_id = await asyncio.to_thread(_grid_session_create)
    got = []
    try:
        bridge = CDPBridge(session_id)
        await bridge.connect()
        t_click = None

        async def on_frame(params):
            got.append((time.monotonic(), len(params["data"])))
            await bridge.ack_frame(params["sessionId"])

        await bridge.start_screencast(on_frame)
        await bridge.navigate("http://caddy:8080/buttons/")
        await asyncio.sleep(3)
        # Screencast is repaint-driven: force continuous visual change.
        await bridge.call(
            "Runtime.evaluate",
            {"expression": "setInterval(() => {document.body.style."
                           "background = '#' + Math.floor(Math.random()*"
                           "16777215).toString(16);}, 300);"})
        await asyncio.sleep(4)
        t_click = time.monotonic()
        await bridge.click(640, 200)
        await asyncio.sleep(3)
        await bridge.close()
    finally:
        await asyncio.to_thread(_grid_session_delete, session_id)
    n = len(got)
    span = (got[-1][0] - got[0][0]) if n > 1 else 0.0
    fps = n / span if span > 0 else 0.0
    sizes = sorted(s for _, s in got)
    print(f"frames={n} span_s={span:.1f} fps={fps:.1f} "
          f"median_bytes={sizes[len(sizes)//2] if sizes else 0}")
    print("OK" if n >= 10 and fps >= 5 else "WEAK")


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
