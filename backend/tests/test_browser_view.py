"""Tests for the CDP-direct viewer mount (no live Grid needed)."""

import unittest
from unittest import mock

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.api.routers import browser_view


class ViewerMountTest(unittest.TestCase):
    def test_viewer_page_serves_html(self):
        client = TestClient(browser_view.create_app())
        resp = client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("CDP direct viewer", resp.text)
        # The start page is baked in: no unfilled placeholder, so a
        # fresh viewer navigates somewhere on connect (black canvas
        # otherwise — a new tab never repaints).
        self.assertNotIn("__DEFAULT_URL__", resp.text)
        self.assertIn(browser_view.DEFAULT_URL, resp.text)

    def test_default_url_is_neutral_not_console(self):
        # Issue #1023: first paint must not recurse into Buttons
        # itself — a neutral page paints immediately and reads
        # sanely. https so no upgrade warning on first paint.
        self.assertNotIn("caddy", browser_view.DEFAULT_URL)
        self.assertTrue(
            browser_view.DEFAULT_URL.startswith("https://"))

    def test_unknown_ws_path_closes(self):
        client = TestClient(browser_view.create_app())
        with self.assertRaises(WebSocketDisconnect):
            with client.websocket_connect("/nope"):
                pass

    def test_attached_names_viewer_kinds(self):
        # Issue #1026: the state line distinguishes agent-shared,
        # viewer-shared, and own sessions for the human.
        self.assertIn("shared browser tab", browser_view.VIEWER_HTML)
        self.assertIn("shared agent session", browser_view.VIEWER_HTML)

    def test_viewer_reconnects_after_socket_close(self):
        # A dropped socket (deploy, Grid restart) must re-dial
        # instead of stranding clicks on a dead canvas; input
        # during the gap is dropped, never thrown.
        html = browser_view.VIEWER_HTML
        self.assertIn("ws.onclose", html)
        self.assertIn("reconnecting", html)
        self.assertIn("setTimeout(connect", html)
        self.assertIn("sendable()", html)
        # A frozen last frame must not pose as live while down.
        self.assertIn("cv.style.opacity = '0.35'", html)
        self.assertIn("cv.style.opacity = ''", html)


class FakeBridge:
    """Stand-in for CDPBridge: no Grid traffic under test."""
    def __init__(self, session_id):
        self.session_id = session_id
        self.dsf = browser_view.DSF
        self.calls = []

    async def connect(self):
        pass

    async def start_screencast(self, on_frame):
        self.calls.append(("screencast",))

    async def navigate(self, url):
        self.calls.append(("navigate", url))

    async def click(self, x, y):
        self.calls.append(("click", x, y))

    async def key(self, key, code):
        self.calls.append(("key", key, code))

    async def reattach(self):
        pass

    async def close(self):
        pass


class SharedViewerSessionTest(unittest.TestCase):
    def setUp(self):
        self._sid = browser_view._viewer_session_id
        self._ref = browser_view._viewer_refcount
        browser_view._viewer_session_id = None
        browser_view._viewer_refcount = 0
        self._bridge = mock.patch.object(
            browser_view, "CDPBridge", FakeBridge)
        self._bridge.start()
        self.addCleanup(self._bridge.stop)
        self.addCleanup(self._restore)

    def _restore(self):
        browser_view._viewer_session_id = self._sid
        browser_view._viewer_refcount = self._ref

    def test_two_tabs_share_one_session(self):
        # Issue #1026: first tab creates, second tab attaches to the
        # same Grid session (one create for two viewers). Teardown
        # policy is covered directly below: the TestClient portal
        # tears down app tasks on close before a second awaited
        # step in the websocket finally can run.
        import json as _json
        client = TestClient(browser_view.create_app())
        with mock.patch.object(
                browser_view, "_grid_live_session",
                return_value=None), \
             mock.patch.object(
                 browser_view, "_grid_session_create",
                 return_value="AAA") as create, \
             mock.patch.object(
                 browser_view, "_grid_session_alive",
                 return_value=True), \
             mock.patch.object(browser_view, "_grid_session_delete"):
            with client.websocket_connect("/ws") as ws1:
                first = _json.loads(ws1.receive_text())
                self.assertEqual(first["kind"], "own")
                with client.websocket_connect("/ws") as ws2:
                    second = _json.loads(ws2.receive_text())
                    self.assertEqual(second["kind"], "viewer-shared")
                    self.assertEqual(second["session"],
                                     first["session"])
            self.assertEqual(create.call_count, 1)

    def _wait_calls(self, shared, want, timeout=10):
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            if want in shared.calls:
                return True
            time.sleep(0.05)
        return False

    def test_click_dispatched_with_human_pause(self):
        # Rank 1: the shared ws-loop point (click -> human pause ->
        # recover_once -> bridge.click) must be wired; a break here
        # kills both modalities at once with no other symptom.
        import json as _json
        client = TestClient(browser_view.create_app())
        shared = FakeBridge("AAA")
        with mock.patch.object(
                browser_view, "CDPBridge",
                return_value=shared), \
             mock.patch.object(
                 browser_view, "_grid_live_session",
                 return_value=None), \
             mock.patch.object(
                 browser_view, "_grid_session_create",
                 return_value="AAA"), \
             mock.patch.object(
                 browser_view, "_grid_session_alive",
                 return_value=True), \
             mock.patch.object(browser_view, "_grid_session_delete"), \
             mock.patch.object(
                 browser_view, "_pause_agent",
                 return_value=False) as paused:
            with client.websocket_connect("/ws") as ws:
                first = _json.loads(ws.receive_text())
                self.assertEqual(first["kind"], "own")
                ws.send_text(_json.dumps({"t": "click",
                                          "x": 960, "y": 200}))
                self.assertTrue(
                    self._wait_calls(shared, ("click", 960, 200)),
                    f"click never reached bridge: {shared.calls}")
                self.assertEqual(paused.call_count, 1)

    def test_key_dispatched_with_human_pause(self):
        import json as _json
        client = TestClient(browser_view.create_app())
        shared = FakeBridge("AAA")
        with mock.patch.object(
                browser_view, "CDPBridge",
                return_value=shared), \
             mock.patch.object(
                 browser_view, "_grid_live_session",
                 return_value=None), \
             mock.patch.object(
                 browser_view, "_grid_session_create",
                 return_value="AAA"), \
             mock.patch.object(
                 browser_view, "_grid_session_alive",
                 return_value=True), \
             mock.patch.object(browser_view, "_grid_session_delete"), \
             mock.patch.object(
                 browser_view, "_pause_agent",
                 return_value=False) as paused:
            with client.websocket_connect("/ws") as ws:
                first = _json.loads(ws.receive_text())
                self.assertEqual(first["kind"], "own")
                ws.send_text(_json.dumps({"t": "key", "key": "a",
                                          "code": "KeyA"}))
                self.assertTrue(
                    self._wait_calls(shared, ("key", "a", "KeyA")),
                    f"key never reached bridge: {shared.calls}")
                self.assertEqual(paused.call_count, 1)

    def test_go_navigates_and_restarts_screencast(self):
        # do_go: navigate, reattach, re-send dsf, restart screencast.
        # The navigate + second screencast prove the path ran.
        import json as _json
        client = TestClient(browser_view.create_app())
        shared = FakeBridge("AAA")
        with mock.patch.object(
                browser_view, "CDPBridge",
                return_value=shared), \
             mock.patch.object(
                 browser_view, "_grid_live_session",
                 return_value=None), \
             mock.patch.object(
                 browser_view, "_grid_session_create",
                 return_value="AAA"), \
             mock.patch.object(
                 browser_view, "_grid_session_alive",
                 return_value=True), \
             mock.patch.object(browser_view, "_grid_session_delete"), \
             mock.patch.object(
                 browser_view, "_pause_agent",
                 return_value=False):
            with client.websocket_connect("/ws") as ws:
                first = _json.loads(ws.receive_text())
                self.assertEqual(first["kind"], "own")
                ws.send_text(_json.dumps(
                    {"t": "go", "url": "https://example.com/x"}))
                self.assertTrue(
                    self._wait_calls(
                        shared, ("navigate", "https://example.com/x")),
                    f"go never navigated: {shared.calls}")
                import time
                deadline = time.time() + 10
                restarts = 0
                while time.time() < deadline:
                    restarts = sum(1 for c in shared.calls
                                   if c == ("screencast",))
                    if restarts >= 2:
                        break
                    time.sleep(0.05)
                self.assertGreaterEqual(
                    restarts, 2,
                    f"screencast not restarted after go: {shared.calls}")

    def test_connect_failure_surfaces_error_and_closes(self):
        # Past 'attached' with no bridge: the viewer must get a
        # named error + close (re-dial) instead of silent darkness.
        import json as _json
        from starlette.websockets import WebSocketDisconnect
        client = TestClient(browser_view.create_app())
        shared = FakeBridge("AAA")

        async def boom():
            raise RuntimeError("grid gone")

        shared.connect = boom
        with mock.patch.object(
                browser_view, "CDPBridge",
                return_value=shared), \
             mock.patch.object(
                 browser_view, "_grid_live_session",
                 return_value=None), \
             mock.patch.object(
                 browser_view, "_grid_session_create",
                 return_value="AAA"), \
             mock.patch.object(
                 browser_view, "_grid_session_alive",
                 return_value=True), \
             mock.patch.object(browser_view, "_grid_session_delete"):
            with client.websocket_connect("/ws") as ws:
                first = _json.loads(ws.receive_text())
                self.assertEqual(first["kind"], "own")
                second = _json.loads(ws.receive_text())
                self.assertEqual(second["type"], "error")
                self.assertIn("browser connect failed",
                              second["msg"])
                with self.assertRaises(WebSocketDisconnect):
                    ws.receive_text()

    def test_teardown_policy(self):
        # The websocket finally only calls _viewer_teardown: last
        # viewer out deletes, agent attaches own nothing, unknown
        # sessions are left alone.
        import asyncio as _aio
        browser_view._viewer_session_id = "AAA"
        browser_view._viewer_refcount = 2
        with mock.patch.object(
                browser_view, "_grid_session_delete") as delete:
            self.assertFalse(_aio.run(
                browser_view._viewer_teardown("AAA", "viewer-shared")))
            self.assertEqual(delete.call_count, 0)
            self.assertTrue(_aio.run(
                browser_view._viewer_teardown("AAA", "own")))
            delete.assert_called_once_with("AAA")
            self.assertFalse(_aio.run(
                browser_view._viewer_teardown("AAA", "own")))
            self.assertFalse(_aio.run(
                browser_view._viewer_teardown("AGENT", "agent")))

    def test_stale_record_creates_fresh(self):
        # A dead stored session is cleared; the viewer creates anew
        # instead of attaching to a corpse.
        import json as _json
        browser_view._viewer_session_id = "ZZZ"
        browser_view._viewer_refcount = 1
        client = TestClient(browser_view.create_app())
        with mock.patch.object(
                browser_view, "_grid_live_session",
                return_value=None), \
             mock.patch.object(
                 browser_view, "_grid_session_alive",
                 return_value=False), \
             mock.patch.object(
                 browser_view, "_grid_session_create",
                 return_value="NEW"), \
             mock.patch.object(browser_view, "_grid_session_delete"):
            with client.websocket_connect("/ws") as ws:
                msg = _json.loads(ws.receive_text())
                self.assertEqual(msg["kind"], "own")
                self.assertEqual(msg["session"], "NEW"[:8])
            # Post-close release is covered by test_teardown_policy:
            # the TestClient portal tears app tasks down on close.

    def test_create_race_falls_back_to_shared(self):
        # Two tabs connecting at once: the loser of the create race
        # attaches to the just-stored session instead of erroring.
        import json as _json
        client = TestClient(browser_view.create_app())
        with mock.patch.object(
                browser_view, "_grid_live_session",
                return_value=None), \
             mock.patch.object(
                 browser_view, "_viewer_claim",
                 side_effect=[None, "AAA"]) as claim, \
             mock.patch.object(
                 browser_view, "_grid_session_create",
                 side_effect=RuntimeError("slot taken")), \
             mock.patch.object(browser_view, "_grid_session_delete"):
            with client.websocket_connect("/ws") as ws:
                msg = _json.loads(ws.receive_text())
                self.assertEqual(msg["kind"], "viewer-shared")
                self.assertEqual(msg["session"], "AAA"[:8])
            self.assertEqual(claim.call_count, 2)

    def test_ws_attempts_grid_session(self):
        # The route is hit and a session is attempted: a dead grid
        # closes the socket instead of hanging the handshake.
        client = TestClient(browser_view.create_app())
        with mock.patch.object(
                browser_view, "_grid_session_create",
                side_effect=RuntimeError("grid down")) as m:
            with client.websocket_connect("/ws") as ws:
                raw = ws.receive_text()
                self.assertIn("browser busy", raw)
                with self.assertRaises(WebSocketDisconnect) as cm:
                    ws.receive_text()
            self.assertEqual(cm.exception.code, 1013)
            m.assert_called_once()

    def test_ws_grid_busy_reports_error(self):
        # Single-slot Grid full: the viewer must SEE "browser busy"
        # instead of hanging on "connecting…" with an ASGI traceback.
        import json as _json
        client = TestClient(browser_view.create_app())
        with mock.patch.object(
                browser_view, "_grid_live_session", return_value=None):
            with mock.patch.object(
                    browser_view, "_grid_session_create",
                    side_effect=RuntimeError("grid full")):
                with client.websocket_connect("/ws") as ws:
                    raw = ws.receive_text()
                    self.assertIn("browser busy", raw)
                    self.assertEqual(
                        _json.loads(raw)["type"], "error")
                    with self.assertRaises(WebSocketDisconnect) as cm:
                        ws.receive_text()
                self.assertEqual(cm.exception.code, 1013)


class LiveSessionFilterTest(unittest.TestCase):
    def test_attaches_only_to_marked_agent_sessions(self):
        import io
        import json
        from unittest import mock
        human = {"sessionId": "human1",
                 "capabilities": {"browserName": "chrome"}}
        agent = {"sessionId": "agent9",
                 "capabilities": {"browserName": "chrome",
                                  browser_view.AGENT_SESSION_MARKER: True}}
        payload = io.BytesIO(json.dumps(
            {"value": {"nodes": [{"slots": [{"session": human},
                                            {"session": agent}]}]}}).encode())

        class Ctx:
            def __enter__(self): return payload
            def __exit__(self, *a): return False

        with mock.patch.object(browser_view.urllib.request, "urlopen",
                               return_value=Ctx()):
            self.assertEqual(browser_view._grid_live_session(), "agent9")

    def test_no_marked_session_means_none(self):
        import io
        import json
        from unittest import mock
        human = {"sessionId": "human1",
                 "capabilities": {"browserName": "chrome"}}
        payload = io.BytesIO(json.dumps(
            {"value": {"nodes": [{"slots": [{"session": human}]}]}}).encode())

        class Ctx:
            def __enter__(self): return payload
            def __exit__(self, *a): return False

        with mock.patch.object(browser_view.urllib.request, "urlopen",
                               return_value=Ctx()):
            self.assertIsNone(browser_view._grid_live_session())


class ReattachTest(unittest.TestCase):
    def test_reattach_binds_current_page_target(self):
        # A cross-origin load can swap the page target: reattach
        # must bind whatever page target exists NOW, not the one
        # from connect time.
        import asyncio
        bridge = browser_view.CDPBridge("sid")
        calls = []

        async def fake_call(method, params=None, timeout=15):
            calls.append((method, params))
            if method == "Target.getTargets":
                return {"targetInfos": [
                    {"type": "browser", "targetId": "B"},
                    {"type": "page", "targetId": "NEW"},
                ]}
            if method == "Target.attachToTarget":
                return {"sessionId": "S9"}
            return {}

        bridge.call = fake_call
        asyncio.run(bridge.reattach())
        self.assertEqual(bridge.cdp_session, "S9")
        attach = [c for c in calls
                  if c[0] == "Target.attachToTarget"]
        self.assertEqual(len(attach), 1)
        self.assertEqual(attach[0][1]["targetId"], "NEW")
        self.assertTrue(attach[0][1]["flatten"])

    def test_reattach_advertises_measured_dpr(self):
        # The override may not apply (live tabs measured 1280px
        # frames with DSF=2 assumed: every click landed at half
        # coords). The bridge measures window.devicePixelRatio per
        # attach and advertises that instead of the assumption.
        import asyncio
        bridge = browser_view.CDPBridge("sid")

        async def fake_call(method, params=None, timeout=15):
            if method == "Target.getTargets":
                return {"targetInfos": [
                    {"type": "page", "targetId": "P"},
                ]}
            if method == "Target.attachToTarget":
                return {"sessionId": "S1"}
            if method == "Runtime.evaluate":
                return {"result": {"value": 1}}
            return {}

        bridge.call = fake_call
        self.assertEqual(bridge.dsf, browser_view.DSF)
        asyncio.run(bridge.reattach())
        self.assertEqual(bridge.dsf, 1.0)

    def test_measure_failure_keeps_assumed_dsf(self):
        import asyncio
        bridge = browser_view.CDPBridge("sid")

        async def fake_call(method, params=None, timeout=15):
            if method == "Target.getTargets":
                return {"targetInfos": [
                    {"type": "page", "targetId": "P"},
                ]}
            if method == "Target.attachToTarget":
                return {"sessionId": "S1"}
            if method == "Runtime.evaluate":
                raise RuntimeError("nope")
            return {}

        bridge.call = fake_call
        asyncio.run(bridge.reattach())
        self.assertEqual(bridge.dsf, browser_view.DSF)

    def test_screencast_sized_from_measured_dsf(self):
        # The capture clamp must match the advertised divisor or
        # frames arrive at a scale the viewer does not know about.
        import asyncio
        bridge = browser_view.CDPBridge("sid")
        seen = {}

        async def fake_call(method, params=None, timeout=15):
            seen[method] = params
            return {}

        bridge.call = fake_call
        bridge.dsf = 2.0
        asyncio.run(bridge.start_screencast(lambda params: None))
        self.assertEqual(seen["Page.startScreencast"]["maxWidth"], 2560)
        self.assertEqual(seen["Page.startScreencast"]["maxHeight"], 1600)
        bridge.dsf = 1.0
        asyncio.run(bridge.start_screencast(lambda params: None))
        self.assertEqual(seen["Page.startScreencast"]["maxWidth"], 1280)
        self.assertEqual(seen["Page.startScreencast"]["maxHeight"], 800)


class BridgeKeyTest(unittest.TestCase):
    def test_unknown_key_raises_instead_of_silence(self):
        # A dropped unknown key looks exactly like dead input;
        # raise so recover_once reports it to the viewer.
        import asyncio
        bridge = browser_view.CDPBridge("sid")

        async def fake_call(method, params=None, timeout=15):
            return {}

        bridge.call = fake_call
        with self.assertRaises(ValueError):
            asyncio.run(bridge.key("Frobnicator", ""))

    def test_focus_failure_does_not_eat_keystroke(self):
        # Focus is best-effort: a failed probe must not abort the
        # key when something is already focused.
        import asyncio
        bridge = browser_view.CDPBridge("sid")
        seen = {}

        async def fake_call(method, params=None, timeout=15):
            seen[method] = params
            if method == "Runtime.evaluate":
                raise RuntimeError("nope")
            return {}

        bridge.call = fake_call
        asyncio.run(bridge.key("a", "KeyA"))
        self.assertEqual(seen.get("Input.insertText"),
                         {"text": "a"})

    def test_reattach_without_page_target_names_it(self):
        # Crash/nav gap with no page target: name it instead of a
        # raw StopIteration.
        import asyncio
        bridge = browser_view.CDPBridge("sid")

        async def fake_call(method, params=None, timeout=15):
            if method == "Target.getTargets":
                return {"targetInfos": [
                    {"type": "browser", "targetId": "B"},
                ]}
            return {}

        bridge.call = fake_call
        with self.assertRaises(RuntimeError):
            asyncio.run(bridge.reattach())


class ScreencastAckTest(unittest.TestCase):
    def test_ack_dispatched_before_frame_callback(self):
        # The compositor blocks until Page.screencastFrameAck arrives:
        # the bridge must ack immediately on receipt, never after the
        # (slow) UI websocket send inside the frame callback.
        import asyncio

        async def go():
            bridge = browser_view.CDPBridge("sid")
            events = []

            class FakeWS:
                async def send(self, raw):
                    import json as _json
                    msg = _json.loads(raw)
                    if msg.get("method") == "Page.screencastFrameAck":
                        events.append("ack")

            bridge.ws = FakeWS()
            bridge.cdp_session = "S1"

            async def on_frame(params):
                events.append("frame")

            bridge._frame_cb = on_frame
            bridge.on_message({"method": "Target.receivedMessageFromTarget",
                               "params": {"message": (
                                   '{"method":"Page.screencastFrame",'
                                   '"params":{"data":"AAA","sessionId":"1"}}')}})
            await asyncio.sleep(0.2)
            self.assertEqual(events[:2], ["ack", "frame"])

        asyncio.run(go())

    def test_rapid_frames_coalesce_to_latest(self):
        # Backpressure: while one UI send is in flight, intermediate
        # frames are dropped and only the latest is flushed next.
        import asyncio

        async def go():
            bridge = browser_view.CDPBridge("sid")
            delivered = []
            release = asyncio.Event()

            class FakeWS:
                async def send(self, raw):
                    pass

            bridge.ws = FakeWS()
            bridge.cdp_session = "S1"

            async def on_frame(params):
                delivered.append(params["data"])
                await release.wait()

            bridge._frame_cb = on_frame
            for data in ("f1", "f2", "f3"):
                bridge.on_message({"method": "Page.screencastFrame",
                                   "params": {"data": data,
                                              "sessionId": "1"}})
                await asyncio.sleep(0.05)
            release.set()
            await asyncio.sleep(0.3)
            # First frame flows, f2 is superseded, latest (f3) follows.
            self.assertEqual(delivered[0], "f1")
            self.assertEqual(delivered[-1], "f3")
            self.assertLessEqual(len(delivered), 2)

        asyncio.run(go())


class CrashRecoveryTest(unittest.TestCase):
    def test_target_crash_invokes_crash_callback(self):
        import asyncio

        async def go():
            bridge = browser_view.CDPBridge("sid")
            called = []

            async def on_crash():
                called.append(1)

            bridge._crash_cb = on_crash
            bridge.on_message({"method": "Inspector.targetCrashed",
                               "params": {}})
            await asyncio.sleep(0.2)
            return called

        self.assertTrue(asyncio.run(go()))


class ViewerHtmlTest(unittest.TestCase):
    def test_dsf_is_global_for_click_mapping(self):
        # pos() maps device pixels back to CSS pixels with dsf: it must
        # resolve at click time, not throw a ReferenceError because dsf
        # was scoped inside the onmessage handler.
        import re
        html = browser_view.VIEWER_HTML
        self.assertIn("function pos(e)", html)
        # A top-level dsf declaration the handler assigns to (no shadowing).
        self.assertTrue(re.search(r"(?m)^(let|var) dsf\s*=", html),
                        "VIEWER_HTML needs a global dsf declaration")
        self.assertNotIn("let dsf = 1;\n    if (m.type === 'attached')", html)

    def test_viewer_applies_measured_dsf(self):
        # The server measures window.devicePixelRatio per attach and
        # sends it as {type: 'dsf'}; the viewer must apply it to the
        # global pos() uses, or clicks land at half coords.
        html = browser_view.VIEWER_HTML
        self.assertIn("if (m.type === 'dsf') { dsf = m.dsf || 1; return; }",
                      html)

    def test_screen_cursor_is_not_crosshair(self):
        # Operator report: the crosshair is awkward to use on the
        # shared screen; the viewer canvas keeps the standard arrow.
        html = browser_view.VIEWER_HTML
        self.assertNotIn("crosshair", html)
        self.assertIn("cursor:default", html.replace(" ", ""))

    def test_url_bar_submits_and_normalizes(self):
        # Enter in the address box must take the same path as Go
        # (form submit, not button click); bare hostnames ride
        # https:// per normalizeUrl.
        html = browser_view.VIEWER_HTML
        self.assertIn('<form id="nav">', html)
        self.assertIn('type="submit"', html)
        self.assertIn("getElementById('nav').onsubmit", html)
        self.assertNotIn("getElementById('go').onclick", html)
        self.assertIn("function normalizeUrl(raw)", html)
        self.assertIn("https://' + v", html)


class ChromeFlagsTest(unittest.TestCase):
    def test_session_args_fallback_off_shm(self):
        # Complement shm_size 2gb: when raster buffers spike, Chromium
        # falls back to /tmp instead of taking a /dev/shm SIGBUS.
        import io
        import json
        from unittest import mock
        seen = {}

        def fake_urlopen(req, timeout=60):
            seen["data"] = json.loads(req.data.decode())
            class Ctx:
                def __enter__(self): return io.BytesIO(
                    b'{"value": {"sessionId": "s1"}}')
                def __exit__(self, *a): return False
            return Ctx()

        with mock.patch.object(browser_view.urllib.request, "urlopen",
                               side_effect=fake_urlopen):
            browser_view._grid_session_create()
        args = seen["data"]["capabilities"]["alwaysMatch"][
            "goog:chromeOptions"]["args"]
        self.assertIn("--disable-dev-shm-usage", args)


if __name__ == "__main__":
    unittest.main()
