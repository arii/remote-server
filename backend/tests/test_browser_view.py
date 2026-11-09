"""Tests for the CDP-direct viewer mount (no live Grid needed)."""

import unittest
from unittest import mock

import cli
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.api.routers import browser_view


class ViewerMountTest(unittest.TestCase):
    def test_viewer_page_serves_html(self):
        client = TestClient(browser_view.create_app())
        resp = client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Browser viewer", resp.text)
        # The start page is baked in: no unfilled placeholder, so a
        # fresh viewer navigates somewhere on connect (black canvas
        # otherwise — a new tab never repaints).
        self.assertNotIn("__DEFAULT_URL__", resp.text)
        self.assertIn(browser_view.DEFAULT_URL, resp.text)

    def test_keyboard_is_floating_overlay_with_fab(self):
        # Research slice 1 (Kasm FAB + draggable panel, Guacamole
        # summon-on-demand): the keyboard must overlay the canvas
        # (position:fixed), never dock in-flow; a coarse-pointer FAB
        # toggles it, a handle drags it, keys meet 48px height.
        html = browser_view.VIEWER_HTML
        self.assertIn('id="kb-fab"', html)
        self.assertIn('id="kbd-handle"', html)
        self.assertIn('id="kbd-close"', html)
        self.assertIn("#kbd{position:fixed", html.replace(" ", ""))
        self.assertIn(
            "@media(pointer:coarse){#kb-fab{display:block}}", html.replace(" ", "")
        )
        self.assertIn("min-height:48px", html.replace(" ", ""))
        self.assertIn("useTouchEvents", html)
        self.assertIn("localStorage.getItem('kbd-pos')", html)
        self.assertNotIn("box.scrollIntoView", html)

    def test_mobile_keyboard_uses_native_bridge(self):
        # Phone audit fixes: no squeezed desktop board on coarse
        # phones — the Keyboard button summons the OS keyboard
        # through a hidden conduit input; the form never shows a
        # native validation bubble; the address bar owns its row.
        html = browser_view.VIEWER_HTML
        flat = html.replace(" ", "")
        self.assertIn('id="native-kb"', html)
        self.assertIn('<form id="nav" novalidate>', html)
        self.assertIn("@media(max-width:600px){", flat)
        self.assertIn("#nav{flex:11100%}", flat)
        self.assertIn("isMobileBoard", html)
        self.assertIn("compositionend", html)
        self.assertIn('enterkeyhint="enter"', html)
        # FAB keeps its coarse-only slot but hides under the open
        # panel instead of overlapping its keys.
        self.assertIn("fab.style.visibility", html)

    def test_viewer_assets_are_vendored_no_cdn(self):
        # The viewer must load with zero external requests: a
        # render-blocking CDN <script> stalls the whole page (and the
        # console iframe's onLoad) whenever the CDN is unreachable —
        # this reddened the console E2E smoke on a CI DNS hiccup.
        html = browser_view.VIEWER_HTML
        self.assertNotIn("cdn.jsdelivr.net", html)
        self.assertNotIn('src="http', html)
        self.assertNotIn('href="http', html)
        self.assertIn("vendor/simple-keyboard-3.8.205.js", html)
        self.assertIn("vendor/simple-keyboard-3.8.205.css", html)
        client = TestClient(browser_view.create_app())
        js = client.get("/vendor/simple-keyboard-3.8.205.js")
        self.assertEqual(js.status_code, 200)
        self.assertIn("javascript", js.headers["content-type"])
        self.assertIn("simple-keyboard v3.8.205", js.text)
        css = client.get("/vendor/simple-keyboard-3.8.205.css")
        self.assertEqual(css.status_code, 200)
        self.assertIn("text/css", css.headers["content-type"])
        self.assertIn("hg-button", css.text)

    def test_viewer_names_busy_slot(self):
        # A live socket with no frames (single Grid slot held
        # elsewhere, or a stuck session) must say so instead of a
        # black canvas that reads as broken. The hint clears on the
        # first frame and never outlives a reconnect.
        html = browser_view.VIEWER_HTML
        self.assertIn("no frames yet", html)
        self.assertIn("st.dataset.label", html)
        self.assertIn("clearTimeout(noFrameTimer)", html)

    def test_viewer_no_store_is_edge_owned(self):
        # No-store lives in the Caddy /cdp-stream* handle, not app
        # code (edge concern, like X-Frame-Options): the app must not
        # set it, and the route must. A cached viewer silently pins
        # old behavior with zero errors to diagnose.
        import pathlib
        import re

        repo_root = pathlib.Path(cli.repo_root())
        caddyfile = (repo_root / "caddy" / "Caddyfile").read_text()
        handle = re.search(r"handle /cdp-stream\* \{(.*?)\n\t\}", caddyfile, re.S)
        self.assertIsNotNone(handle, "missing /cdp-stream* handle")
        self.assertIn("no-store", handle.group(1))
        self.assertNotIn("no-store", browser_view.VIEWER_HTML)
        client = TestClient(browser_view.create_app())
        resp = client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("no-store", resp.headers.get("cache-control", ""))

    def test_default_url_is_neutral_not_console(self):
        # Issue #1023: first paint must not recurse into Buttons
        # itself — a neutral page paints immediately and reads
        # sanely. https so no upgrade warning on first paint.
        self.assertNotIn("caddy", browser_view.DEFAULT_URL)
        self.assertTrue(browser_view.DEFAULT_URL.startswith("https://"))

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

    def __init__(self, session_id, viewport=None, mobile=False):
        self.session_id = session_id
        self.viewport = (
            dict(viewport) if viewport is not None else dict(browser_view.VIEWPORT)
        )
        self.mobile = mobile
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

    async def move(self, x, y):
        self.calls.append(("move", x, y))

    async def key(self, key, code):
        self.calls.append(("key", key, code))

    async def type_text(self, text):
        self.calls.append(("type_text", text))

    async def wheel(self, x, y, dx, dy):
        self.calls.append(("wheel", x, y, dx, dy))

    async def touch(self, phase, x, y):
        self.calls.append(("touch", phase, x, y))

    async def reattach(self):
        pass

    async def set_viewport_mode(self, mobile):
        self.mobile = bool(mobile)
        self.viewport = dict(
            browser_view.MOBILE_VIEWPORT if self.mobile else browser_view.VIEWPORT
        )
        self.calls.append(("viewport", self.mobile))
        await self.reattach()

    async def close(self):
        pass


class SharedViewerSessionTest(unittest.TestCase):
    def setUp(self):
        self._sid = browser_view._viewer_session_id
        self._ref = browser_view._viewer_refcount
        self._vp = browser_view._viewer_viewport
        browser_view._viewer_session_id = None
        browser_view._viewer_refcount = 0
        browser_view._viewer_viewport = None
        self._bridge = mock.patch.object(browser_view, "CDPBridge", FakeBridge)
        self._bridge.start()
        self.addCleanup(self._bridge.stop)
        self.addCleanup(self._restore)

    def _restore(self):
        browser_view._viewer_session_id = self._sid
        browser_view._viewer_refcount = self._ref
        browser_view._viewer_viewport = self._vp

    def test_two_tabs_share_one_session(self):
        # Issue #1026: first tab creates, second tab attaches to the
        # same Grid session (one create for two viewers). Teardown
        # policy is covered directly below: the TestClient portal
        # tears down app tasks on close before a second awaited
        # step in the websocket finally can run.
        import json as _json

        client = TestClient(browser_view.create_app())
        with (
            mock.patch.object(browser_view, "_grid_live_session", return_value=None),
            mock.patch.object(
                browser_view, "_grid_session_create", return_value="AAA"
            ) as create,
            mock.patch.object(browser_view, "_grid_session_alive", return_value=True),
            mock.patch.object(browser_view, "_grid_session_delete"),
        ):
            with client.websocket_connect("/ws") as ws1:
                first = _json.loads(ws1.receive_text())
                self.assertEqual(first["kind"], "own")
                with client.websocket_connect("/ws") as ws2:
                    second = _json.loads(ws2.receive_text())
                    self.assertEqual(second["kind"], "viewer-shared")
                    self.assertEqual(second["session"], first["session"])
            self.assertEqual(create.call_count, 1)

    def _wait_calls(self, shared, want, timeout=10):
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            if want in shared.calls:
                return True
            time.sleep(0.05)
        return False

    def _wait_count(self, mocked, want, timeout=10):
        # The pause call runs on a worker thread via a fire-and-
        # forget task: poll for it instead of asserting a race.
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            if mocked.call_count >= want:
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
        with (
            mock.patch.object(browser_view, "CDPBridge", return_value=shared),
            mock.patch.object(browser_view, "_grid_live_session", return_value=None),
            mock.patch.object(browser_view, "_grid_session_create", return_value="AAA"),
            mock.patch.object(browser_view, "_grid_session_alive", return_value=True),
            mock.patch.object(browser_view, "_grid_session_delete"),
            mock.patch.object(
                browser_view, "_pause_agent", return_value=False
            ) as paused,
        ):
            with client.websocket_connect("/ws") as ws:
                first = _json.loads(ws.receive_text())
                self.assertEqual(first["kind"], "own")
                ws.send_text(_json.dumps({"t": "click", "x": 960, "y": 200}))
                self.assertTrue(
                    self._wait_calls(shared, ("click", 960, 200)),
                    f"click never reached bridge: {shared.calls}",
                )
                self.assertTrue(self._wait_count(paused, 1), "pause task never ran")
                self.assertEqual(paused.call_count, 1)

    def test_key_dispatched_with_human_pause(self):
        import json as _json

        client = TestClient(browser_view.create_app())
        shared = FakeBridge("AAA")
        with (
            mock.patch.object(browser_view, "CDPBridge", return_value=shared),
            mock.patch.object(browser_view, "_grid_live_session", return_value=None),
            mock.patch.object(browser_view, "_grid_session_create", return_value="AAA"),
            mock.patch.object(browser_view, "_grid_session_alive", return_value=True),
            mock.patch.object(browser_view, "_grid_session_delete"),
            mock.patch.object(
                browser_view, "_pause_agent", return_value=False
            ) as paused,
        ):
            with client.websocket_connect("/ws") as ws:
                first = _json.loads(ws.receive_text())
                self.assertEqual(first["kind"], "own")
                ws.send_text(_json.dumps({"t": "key", "key": "a", "code": "KeyA"}))
                self.assertTrue(
                    self._wait_calls(shared, ("key", "a", "KeyA")),
                    f"key never reached bridge: {shared.calls}",
                )
                self.assertTrue(self._wait_count(paused, 1), "pause task never ran")
                self.assertEqual(paused.call_count, 1)

    def test_viewport_toggle_dispatched_without_human_pause(self):
        # Mobile/desktop toggle is look-around input like wheel: it
        # reaches the bridge without tripping agent-takeover pause.
        import json as _json

        client = TestClient(browser_view.create_app())
        shared = FakeBridge("AAA")
        with (
            mock.patch.object(browser_view, "CDPBridge", return_value=shared),
            mock.patch.object(browser_view, "_grid_live_session", return_value=None),
            mock.patch.object(browser_view, "_grid_session_create", return_value="AAA"),
            mock.patch.object(browser_view, "_grid_session_alive", return_value=True),
            mock.patch.object(browser_view, "_grid_session_delete"),
            mock.patch.object(
                browser_view, "_pause_agent", return_value=False
            ) as paused,
        ):
            with client.websocket_connect("/ws") as ws:
                first = _json.loads(ws.receive_text())
                self.assertEqual(first["kind"], "own")
                ws.send_text(_json.dumps({"t": "viewport", "mode": "mobile"}))
                self.assertTrue(
                    self._wait_calls(shared, ("viewport", True)),
                    f"viewport never reached bridge: {shared.calls}",
                )
                self.assertEqual(paused.call_count, 0)

    def test_wheel_dispatched_without_human_pause(self):
        # Scroll is look-around input like a hover: it must reach the
        # bridge without tripping the agent-takeover pause.
        import json as _json

        client = TestClient(browser_view.create_app())
        shared = FakeBridge("AAA")
        with (
            mock.patch.object(browser_view, "CDPBridge", return_value=shared),
            mock.patch.object(browser_view, "_grid_live_session", return_value=None),
            mock.patch.object(browser_view, "_grid_session_create", return_value="AAA"),
            mock.patch.object(browser_view, "_grid_session_alive", return_value=True),
            mock.patch.object(browser_view, "_grid_session_delete"),
            mock.patch.object(
                browser_view, "_pause_agent", return_value=False
            ) as paused,
        ):
            with client.websocket_connect("/ws") as ws:
                first = _json.loads(ws.receive_text())
                self.assertEqual(first["kind"], "own")
                ws.send_text(
                    _json.dumps({"t": "wheel", "x": 10, "y": 20, "dx": 0, "dy": 120})
                )
                self.assertTrue(
                    self._wait_calls(shared, ("wheel", 10, 20, 0, 120)),
                    f"wheel never reached bridge: {shared.calls}",
                )
                self.assertEqual(paused.call_count, 0)

    def test_move_dispatched_without_human_pause(self):
        # Hover is look-around input like the wheel: it must reach
        # the bridge without tripping the agent-takeover pause.
        import json as _json

        client = TestClient(browser_view.create_app())
        shared = FakeBridge("AAA")
        with (
            mock.patch.object(browser_view, "CDPBridge", return_value=shared),
            mock.patch.object(browser_view, "_grid_live_session", return_value=None),
            mock.patch.object(browser_view, "_grid_session_create", return_value="AAA"),
            mock.patch.object(browser_view, "_grid_session_alive", return_value=True),
            mock.patch.object(browser_view, "_grid_session_delete"),
            mock.patch.object(
                browser_view, "_pause_agent", return_value=False
            ) as paused,
        ):
            with client.websocket_connect("/ws") as ws:
                first = _json.loads(ws.receive_text())
                self.assertEqual(first["kind"], "own")
                ws.send_text(_json.dumps({"t": "move", "x": 640, "y": 400}))
                self.assertTrue(
                    self._wait_calls(shared, ("move", 640, 400)),
                    f"move never reached bridge: {shared.calls}",
                )
                self.assertEqual(paused.call_count, 0)

    def test_malformed_frames_do_not_kill_socket(self):
        # One bad frame (non-dict JSON, missing coords, missing
        # url) must not raise out of the receive loop: the next
        # good input still lands and the socket stays open.
        import json as _json

        client = TestClient(browser_view.create_app())
        shared = FakeBridge("AAA")
        with (
            mock.patch.object(browser_view, "CDPBridge", return_value=shared),
            mock.patch.object(browser_view, "_grid_live_session", return_value=None),
            mock.patch.object(browser_view, "_grid_session_create", return_value="AAA"),
            mock.patch.object(browser_view, "_grid_session_alive", return_value=True),
            mock.patch.object(browser_view, "_grid_session_delete"),
            mock.patch.object(browser_view, "_pause_agent", return_value=True),
        ):
            with client.websocket_connect("/ws") as ws:
                first = _json.loads(ws.receive_text())
                self.assertEqual(first["kind"], "own")
                ws.send_text("[1, 2]")
                ws.send_text(_json.dumps({"t": "click"}))
                ws.send_text(_json.dumps({"t": "go"}))
                ws.send_text(_json.dumps({"t": "click", "x": 7, "y": 9}))
                self.assertTrue(
                    self._wait_calls(shared, ("click", 7, 9)),
                    f"socket died on malformed input: {shared.calls}",
                )

    def test_pause_failure_rearms_next_input(self):
        # A transient pause-API failure must not latch the session
        # unpaused-forever: the next input retries the pause call.
        import json as _json
        import time as _time

        client = TestClient(browser_view.create_app())
        shared = FakeBridge("AAA")
        with (
            mock.patch.object(browser_view, "CDPBridge", return_value=shared),
            mock.patch.object(browser_view, "_grid_live_session", return_value=None),
            mock.patch.object(browser_view, "_grid_session_create", return_value="AAA"),
            mock.patch.object(browser_view, "_grid_session_alive", return_value=True),
            mock.patch.object(browser_view, "_grid_session_delete"),
            mock.patch.object(
                browser_view, "_pause_agent", side_effect=[False, True]
            ) as paused,
        ):
            with client.websocket_connect("/ws") as ws:
                first = _json.loads(ws.receive_text())
                self.assertEqual(first["kind"], "own")
                ws.send_text(_json.dumps({"t": "click", "x": 1, "y": 1}))
                deadline = _time.time() + 10
                while paused.call_count < 1 and _time.time() < deadline:
                    _time.sleep(0.05)
                self.assertEqual(paused.call_count, 1)
                ws.send_text(_json.dumps({"t": "click", "x": 2, "y": 2}))
                deadline = _time.time() + 10
                while paused.call_count < 2 and _time.time() < deadline:
                    _time.sleep(0.05)
                self.assertEqual(
                    paused.call_count, 2, "failed pause did not re-arm the next input"
                )

    def test_viewer_shared_adopts_owner_viewport(self):
        # A desktop tab joining a phone-owned session adopts the
        # owner's 390px mobile emulation instead of re-emulating
        # the shared Grid session to 1280px (metrics flap for all).
        import json as _json

        phone_ua = (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like "
            "Mac OS X) AppleWebKit/605.1.15 (KHTML, like "
            "Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
        )
        made = []

        def factory(session_id, viewport=None, mobile=False):
            bridge = FakeBridge(session_id, viewport=viewport, mobile=mobile)
            made.append(bridge)
            return bridge

        client = TestClient(browser_view.create_app())
        with (
            mock.patch.object(browser_view, "CDPBridge", side_effect=factory),
            mock.patch.object(browser_view, "_grid_live_session", return_value=None),
            mock.patch.object(browser_view, "_grid_session_create", return_value="AAA"),
            mock.patch.object(browser_view, "_grid_session_alive", return_value=True),
            mock.patch.object(browser_view, "_grid_session_delete"),
            mock.patch.object(browser_view, "_pause_agent", return_value=True),
        ):
            with client.websocket_connect(
                "/ws", headers={"user-agent": phone_ua}
            ) as ws1:
                first = _json.loads(ws1.receive_text())
                self.assertEqual(first["kind"], "own")
                self.assertTrue(made[0].mobile)
                with client.websocket_connect("/ws") as ws2:
                    second = _json.loads(ws2.receive_text())
                    self.assertEqual(second["kind"], "viewer-shared")
                    self.assertEqual(second["session"], first["session"])
                self.assertTrue(
                    made[1].mobile, f"joiner kept desktop emulation: {made[1].viewport}"
                )
                self.assertEqual(made[1].viewport, made[0].viewport)

    def test_touch_sequence_dispatched(self):
        # One-finger drag: touchstart -> touchmove -> touchend.
        import json as _json

        client = TestClient(browser_view.create_app())
        shared = FakeBridge("AAA")
        with (
            mock.patch.object(browser_view, "CDPBridge", return_value=shared),
            mock.patch.object(browser_view, "_grid_live_session", return_value=None),
            mock.patch.object(browser_view, "_grid_session_create", return_value="AAA"),
            mock.patch.object(browser_view, "_grid_session_alive", return_value=True),
            mock.patch.object(browser_view, "_grid_session_delete"),
            mock.patch.object(browser_view, "_pause_agent", return_value=False),
        ):
            with client.websocket_connect("/ws") as ws:
                first = _json.loads(ws.receive_text())
                self.assertEqual(first["kind"], "own")
                ws.send_text(
                    _json.dumps({"t": "touch", "phase": "start", "x": 100, "y": 200})
                )
                ws.send_text(
                    _json.dumps({"t": "touch", "phase": "move", "x": 100, "y": 100})
                )
                ws.send_text(
                    _json.dumps({"t": "touch", "phase": "end", "x": 100, "y": 100})
                )
                ws.send_text(
                    _json.dumps({"t": "touch", "phase": "pinch", "x": 0, "y": 0})
                )
                self.assertTrue(
                    self._wait_calls(shared, ("touch", "end", 100, 100)),
                    f"touch never reached bridge: {shared.calls}",
                )
                kinds = [c[1] for c in shared.calls if c[0] == "touch"]
                self.assertEqual(kinds, ["start", "move", "end"])

    def test_type_dispatched_with_human_pause(self):
        # On-screen keyboard bulk commit (type -> human pause ->
        # recover_once -> bridge.type_text) must be wired; printable
        # mobile input has no other path to the remote page.
        import json as _json

        client = TestClient(browser_view.create_app())
        shared = FakeBridge("AAA")
        with (
            mock.patch.object(browser_view, "CDPBridge", return_value=shared),
            mock.patch.object(browser_view, "_grid_live_session", return_value=None),
            mock.patch.object(browser_view, "_grid_session_create", return_value="AAA"),
            mock.patch.object(browser_view, "_grid_session_alive", return_value=True),
            mock.patch.object(browser_view, "_grid_session_delete"),
            mock.patch.object(
                browser_view, "_pause_agent", return_value=False
            ) as paused,
        ):
            with client.websocket_connect("/ws") as ws:
                first = _json.loads(ws.receive_text())
                self.assertEqual(first["kind"], "own")
                ws.send_text(_json.dumps({"t": "type", "text": "hello"}))
                self.assertTrue(
                    self._wait_calls(shared, ("type_text", "hello")),
                    f"type never reached bridge: {shared.calls}",
                )
                self.assertTrue(self._wait_count(paused, 1), "pause task never ran")
                self.assertEqual(paused.call_count, 1)

    def test_go_navigates_and_restarts_screencast(self):
        # do_go: navigate, reattach, re-send dsf, restart screencast.
        # The navigate + second screencast prove the path ran.
        import json as _json

        client = TestClient(browser_view.create_app())
        shared = FakeBridge("AAA")
        with (
            mock.patch.object(browser_view, "CDPBridge", return_value=shared),
            mock.patch.object(browser_view, "_grid_live_session", return_value=None),
            mock.patch.object(browser_view, "_grid_session_create", return_value="AAA"),
            mock.patch.object(browser_view, "_grid_session_alive", return_value=True),
            mock.patch.object(browser_view, "_grid_session_delete"),
            mock.patch.object(browser_view, "_pause_agent", return_value=False),
        ):
            with client.websocket_connect("/ws") as ws:
                first = _json.loads(ws.receive_text())
                self.assertEqual(first["kind"], "own")
                ws.send_text(_json.dumps({"t": "go", "url": "https://example.com/x"}))
                self.assertTrue(
                    self._wait_calls(shared, ("navigate", "https://example.com/x")),
                    f"go never navigated: {shared.calls}",
                )
                import time

                deadline = time.time() + 10
                restarts = 0
                while time.time() < deadline:
                    restarts = sum(1 for c in shared.calls if c == ("screencast",))
                    if restarts >= 2:
                        break
                    time.sleep(0.05)
                self.assertGreaterEqual(
                    restarts, 2, f"screencast not restarted after go: {shared.calls}"
                )

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
        with (
            mock.patch.object(browser_view, "CDPBridge", return_value=shared),
            mock.patch.object(browser_view, "_grid_live_session", return_value=None),
            mock.patch.object(browser_view, "_grid_session_create", return_value="AAA"),
            mock.patch.object(browser_view, "_grid_session_alive", return_value=True),
            mock.patch.object(browser_view, "_grid_session_delete"),
        ):
            with client.websocket_connect("/ws") as ws:
                first = _json.loads(ws.receive_text())
                self.assertEqual(first["kind"], "own")
                second = _json.loads(ws.receive_text())
                self.assertEqual(second["type"], "error")
                self.assertIn("browser connect failed", second["msg"])
                with self.assertRaises(WebSocketDisconnect):
                    ws.receive_text()

    def test_teardown_policy(self):
        # The websocket finally only calls _viewer_teardown: last
        # viewer out deletes, agent attaches own nothing, unknown
        # sessions are left alone.
        import asyncio as _aio

        browser_view._viewer_session_id = "AAA"
        browser_view._viewer_refcount = 2
        with mock.patch.object(browser_view, "_grid_session_delete") as delete:
            self.assertFalse(
                _aio.run(browser_view._viewer_teardown("AAA", "viewer-shared"))
            )
            self.assertEqual(delete.call_count, 0)
            self.assertTrue(_aio.run(browser_view._viewer_teardown("AAA", "own")))
            delete.assert_called_once_with("AAA")
            self.assertFalse(_aio.run(browser_view._viewer_teardown("AAA", "own")))
            self.assertFalse(_aio.run(browser_view._viewer_teardown("AGENT", "agent")))

    def test_viewport_record_follows_session_lifecycle(self):
        # The canonical viewport is stored at create, served only
        # for the recorded session, and cleared with it — a new
        # owner never inherits a stale emulation.
        browser_view._viewer_create("AAA", {"width": 390, "height": 844}, True)
        self.assertEqual(
            browser_view._viewer_viewport_for("AAA"),
            ({"width": 390, "height": 844}, True),
        )
        self.assertIsNone(browser_view._viewer_viewport_for("BBB"))
        self.assertTrue(browser_view._viewer_release("AAA"))
        self.assertIsNone(browser_view._viewer_viewport_for("AAA"))

    def test_stale_record_creates_fresh(self):
        # A dead stored session is cleared; the viewer creates anew
        # instead of attaching to a corpse.
        import json as _json

        browser_view._viewer_session_id = "ZZZ"
        browser_view._viewer_refcount = 1
        client = TestClient(browser_view.create_app())
        with (
            mock.patch.object(browser_view, "_grid_live_session", return_value=None),
            mock.patch.object(browser_view, "_grid_session_alive", return_value=False),
            mock.patch.object(browser_view, "_grid_session_create", return_value="NEW"),
            mock.patch.object(browser_view, "_grid_session_delete"),
        ):
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
        with (
            mock.patch.object(browser_view, "_grid_live_session", return_value=None),
            mock.patch.object(
                browser_view, "_viewer_claim", side_effect=[None, "AAA"]
            ) as claim,
            mock.patch.object(
                browser_view,
                "_grid_session_create",
                side_effect=RuntimeError("slot taken"),
            ),
            mock.patch.object(browser_view, "_grid_session_delete"),
        ):
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
            browser_view, "_grid_session_create", side_effect=RuntimeError("grid down")
        ) as m:
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
        with mock.patch.object(browser_view, "_grid_live_session", return_value=None):
            with mock.patch.object(
                browser_view,
                "_grid_session_create",
                side_effect=RuntimeError("grid full"),
            ):
                with client.websocket_connect("/ws") as ws:
                    raw = ws.receive_text()
                    self.assertIn("browser busy", raw)
                    self.assertEqual(_json.loads(raw)["type"], "error")
                    with self.assertRaises(WebSocketDisconnect) as cm:
                        ws.receive_text()
                self.assertEqual(cm.exception.code, 1013)


class LiveSessionFilterTest(unittest.TestCase):
    def test_attaches_only_to_marked_agent_sessions(self):
        import io
        import json
        from unittest import mock

        human = {"sessionId": "human1", "capabilities": {"browserName": "chrome"}}
        agent = {
            "sessionId": "agent9",
            "capabilities": {
                "browserName": "chrome",
                browser_view.AGENT_SESSION_MARKER: True,
            },
        }
        payload = io.BytesIO(
            json.dumps(
                {
                    "value": {
                        "nodes": [{"slots": [{"session": human}, {"session": agent}]}]
                    }
                }
            ).encode()
        )

        class Ctx:
            def __enter__(self):
                return payload

            def __exit__(self, *a):
                return False

        with mock.patch.object(
            browser_view.urllib.request, "urlopen", return_value=Ctx()
        ):
            self.assertEqual(browser_view._grid_live_session(), "agent9")

    def test_no_marked_session_means_none(self):
        import io
        import json
        from unittest import mock

        human = {"sessionId": "human1", "capabilities": {"browserName": "chrome"}}
        payload = io.BytesIO(
            json.dumps({"value": {"nodes": [{"slots": [{"session": human}]}]}}).encode()
        )

        class Ctx:
            def __enter__(self):
                return payload

            def __exit__(self, *a):
                return False

        with mock.patch.object(
            browser_view.urllib.request, "urlopen", return_value=Ctx()
        ):
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
                return {
                    "targetInfos": [
                        {"type": "browser", "targetId": "B"},
                        {"type": "page", "targetId": "NEW"},
                    ]
                }
            if method == "Target.attachToTarget":
                return {"sessionId": "S9"}
            return {}

        bridge.call = fake_call
        asyncio.run(bridge.reattach())
        self.assertEqual(bridge.cdp_session, "S9")
        attach = [c for c in calls if c[0] == "Target.attachToTarget"]
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
                return {
                    "targetInfos": [
                        {"type": "page", "targetId": "P"},
                    ]
                }
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
                return {
                    "targetInfos": [
                        {"type": "page", "targetId": "P"},
                    ]
                }
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
        self.assertEqual(seen.get("Input.insertText"), {"text": "a"})

    def test_shift_dispatches_raw_with_modifier_mask(self):
        # The viewer banner showed "unknown key: 'Shift'": a bare
        # physical Shift must travel as rawKeyDown/rawKeyUp with
        # the Shift bit set, never raise.
        import asyncio

        bridge = browser_view.CDPBridge("sid")
        calls = []

        async def fake_call(method, params=None, timeout=15):
            if method == "Runtime.evaluate":
                return "BODY"
            calls.append((method, params))
            return {}

        bridge.call = fake_call
        asyncio.run(bridge.key("Shift", "ShiftLeft"))
        key_calls = [c for c in calls if c[0] == "Input.dispatchKeyEvent"]
        self.assertEqual(
            [(c[1]["type"], c[1]["modifiers"]) for c in key_calls],
            [("rawKeyDown", 8), ("rawKeyUp", 0)],
        )
        self.assertEqual(key_calls[0][1]["windowsVirtualKeyCode"], 16)
        self.assertEqual(key_calls[0][1]["code"], "ShiftLeft")

    def test_held_shift_applies_to_chorded_key(self):
        # Shift held while Enter is struck: the Enter keyDown must
        # carry modifiers=8 so Chromium resolves the chord.
        import asyncio

        bridge = browser_view.CDPBridge("sid")
        calls = []

        async def fake_call(method, params=None, timeout=15):
            if method == "Runtime.evaluate":
                return "BODY"
            calls.append((method, params))
            return {}

        bridge.call = fake_call
        asyncio.run(bridge.key("Shift", "ShiftLeft"))
        # A full down/up cycle must leave no leaked modifier state.
        self.assertEqual(bridge._modifiers, 0)
        # A held modifier propagates to the next chorded keyDown.
        calls.clear()
        bridge._modifiers = 8
        asyncio.run(bridge.key("Enter", "Enter"))
        downs = [
            c
            for c in calls
            if c[0] == "Input.dispatchKeyEvent" and c[1]["type"] == "keyDown"
        ]
        self.assertEqual(downs[0][1]["modifiers"], 8)

    def test_viewport_mode_swaps_metrics_and_touch(self):
        # Mobile/desktop toggle: metrics follow the mode and the
        # bridge reattaches so the live target picks them up.
        import asyncio

        bridge = browser_view.CDPBridge("sid")
        reattached = []

        async def fake_reattach():
            reattached.append(True)

        bridge.reattach = fake_reattach
        asyncio.run(bridge.set_viewport_mode(True))
        self.assertTrue(bridge.mobile)
        self.assertEqual(bridge.viewport, browser_view.MOBILE_VIEWPORT)
        asyncio.run(bridge.set_viewport_mode(False))
        self.assertFalse(bridge.mobile)
        self.assertEqual(bridge.viewport, browser_view.VIEWPORT)
        self.assertEqual(len(reattached), 2)

    def test_type_text_bulk_commits_via_insert_text(self):
        # On-screen keyboard text never existed as key presses, so
        # it must take the protocol-blessed IME/commit path whole.
        import asyncio

        bridge = browser_view.CDPBridge("sid")
        seen = {}

        async def fake_call(method, params=None, timeout=15):
            seen[method] = params
            return {}

        bridge.call = fake_call
        asyncio.run(bridge.type_text("hello world"))
        self.assertEqual(seen.get("Input.insertText"), {"text": "hello world"})

    def test_move_sends_mouse_moved_without_buttons(self):
        import asyncio

        bridge = browser_view.CDPBridge("sid")
        seen = {}

        async def fake_call(method, params=None, timeout=15):
            seen[method] = params
            return {}

        bridge.call = fake_call
        asyncio.run(bridge.move(640, 400))
        self.assertEqual(
            seen.get("Input.dispatchMouseEvent"),
            {"x": 640, "y": 400, "type": "mouseMoved"},
        )

    def test_wheel_sends_mouse_wheel_deltas(self):
        import asyncio

        bridge = browser_view.CDPBridge("sid")
        seen = {}

        async def fake_call(method, params=None, timeout=15):
            seen[method] = params
            return {}

        bridge.call = fake_call
        asyncio.run(bridge.wheel(10, 20, 0, 120))
        self.assertEqual(
            seen.get("Input.dispatchMouseEvent"),
            {"x": 10, "y": 20, "type": "mouseWheel", "deltaX": 0, "deltaY": 120},
        )

    def test_touch_sequence_uses_touch_events(self):
        import asyncio

        bridge = browser_view.CDPBridge("sid")
        seen = []

        async def fake_call(method, params=None, timeout=15):
            seen.append((method, params))
            return {}

        bridge.call = fake_call
        asyncio.run(bridge.touch("start", 100, 200))
        asyncio.run(bridge.touch("move", 100, 100))
        asyncio.run(bridge.touch("end", 100, 100))
        self.assertEqual([m for m, _ in seen], ["Input.dispatchTouchEvent"] * 3)
        self.assertEqual(seen[0][1]["type"], "touchStart")
        self.assertEqual(seen[1][1]["type"], "touchMove")
        self.assertEqual(seen[2][1], {"type": "touchEnd", "touchPoints": []})

    def test_reattach_without_page_target_names_it(self):
        # Crash/nav gap with no page target: name it instead of a
        # raw StopIteration.
        import asyncio

        bridge = browser_view.CDPBridge("sid")

        async def fake_call(method, params=None, timeout=15):
            if method == "Target.getTargets":
                return {
                    "targetInfos": [
                        {"type": "browser", "targetId": "B"},
                    ]
                }
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
            bridge.on_message(
                {
                    "method": "Target.receivedMessageFromTarget",
                    "params": {
                        "message": (
                            '{"method":"Page.screencastFrame",'
                            '"params":{"data":"AAA","sessionId":"1"}}'
                        )
                    },
                }
            )
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
                bridge.on_message(
                    {
                        "method": "Page.screencastFrame",
                        "params": {"data": data, "sessionId": "1"},
                    }
                )
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
            bridge.on_message({"method": "Inspector.targetCrashed", "params": {}})
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
        self.assertTrue(
            re.search(r"(?m)^(let|var) dsf\s*=", html),
            "VIEWER_HTML needs a global dsf declaration",
        )
        self.assertNotIn("let dsf = 1;\n    if (m.type === 'attached')", html)

    def test_viewer_applies_measured_dsf(self):
        # The server measures window.devicePixelRatio per attach and
        # sends it as {type: 'dsf'}; the viewer must apply it to the
        # global pos() uses, or clicks land at half coords.
        html = browser_view.VIEWER_HTML
        self.assertIn("if (m.type === 'dsf') { dsf = m.dsf || 1; return; }", html)

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
        # novalidate: the browser's own "enter a URL" bubble
        # obscured controls on phones; bad input surfaces as the
        # remote page's error instead. Submit path unchanged.
        self.assertIn('<form id="nav" novalidate>', html)
        self.assertIn('type="submit"', html)
        self.assertIn("getElementById('nav').onsubmit", html)
        self.assertNotIn("getElementById('go').onclick", html)
        self.assertIn("function normalizeUrl(raw)", html)
        self.assertIn("https://' + v", html)


class MobileViewportTest(unittest.TestCase):
    def setUp(self):
        # Module-global isolation like SharedViewerSessionTest: a
        # stale record must never reach the real _viewer_claim
        # network path from a viewport unit test.
        self._sid = browser_view._viewer_session_id
        self._ref = browser_view._viewer_refcount
        self._vp = browser_view._viewer_viewport
        browser_view._viewer_session_id = None
        browser_view._viewer_refcount = 0
        browser_view._viewer_viewport = None
        self.addCleanup(self._restore)

    def _restore(self):
        browser_view._viewer_session_id = self._sid
        browser_view._viewer_refcount = self._ref
        browser_view._viewer_viewport = self._vp

    PHONE_UAS = (
        # iPhone Safari.
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
            "Mobile/15E148 Safari/604.1"
        ),
        # Android Chrome.
        (
            "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"
        ),
        # iPad Safari.
        (
            "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
            "Mobile/15E148 Safari/604.1"
        ),
    )
    DESKTOP_UA = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )

    def test_phone_ua_gets_mobile_viewport_with_touch(self):
        for ua in self.PHONE_UAS:
            viewport, mobile = browser_view.viewport_for("own", ua)
            self.assertEqual(viewport, {"width": 390, "height": 844}, ua)
            self.assertTrue(mobile, ua)

    def test_desktop_ua_keeps_desktop_viewport(self):
        viewport, mobile = browser_view.viewport_for("own", self.DESKTOP_UA)
        self.assertEqual(viewport, browser_view.VIEWPORT)
        self.assertFalse(mobile)

    def test_shared_and_agent_kinds_stay_desktop_on_phones(self):
        # Every tab on a shared/agent session sees the same page:
        # a phone joining one must not shrink the emulation.
        for kind in ("agent", "viewer-shared"):
            viewport, mobile = browser_view.viewport_for(kind, self.PHONE_UAS[0])
            self.assertEqual(viewport, browser_view.VIEWPORT)
            self.assertFalse(mobile)

    def test_viewer_html_has_viewport_meta(self):
        # Without this, phones shrink the 1280px layout to fit
        # instead of viewing it at device width.
        self.assertIn(
            '<meta name="viewport" '
            'content="width=device-width, initial-scale=1, '
            'viewport-fit=cover">',
            browser_view.VIEWER_HTML,
        )

    def test_bridge_reattach_uses_mobile_viewport_and_touch(self):
        # The bridge carries its viewport: metrics + touch follow it.
        import asyncio

        bridge = browser_view.CDPBridge(
            "sid", viewport=dict(browser_view.MOBILE_VIEWPORT), mobile=True
        )

        async def fake_call(method, params=None, timeout=15):
            calls.append((method, params))
            if method == "Target.getTargets":
                return {
                    "targetInfos": [
                        {"type": "page", "targetId": "P"},
                    ]
                }
            if method == "Target.attachToTarget":
                return {"sessionId": "S1"}
            if method == "Runtime.evaluate":
                return {"result": {"value": 1}}
            return {}

        calls = []
        bridge.call = fake_call
        asyncio.run(bridge.reattach())
        metrics = [p for m, p in calls if m == "Emulation.setDeviceMetricsOverride"]
        self.assertEqual(len(metrics), 1)
        self.assertEqual(metrics[0]["width"], 390)
        self.assertEqual(metrics[0]["height"], 844)
        self.assertTrue(metrics[0]["mobile"])
        touch = [p for m, p in calls if m == "Emulation.setTouchEmulationEnabled"]
        self.assertEqual(touch, [{"enabled": True}])

    def test_bridge_desktop_skips_touch_emulation(self):
        import asyncio

        bridge = browser_view.CDPBridge("sid")
        calls = []

        async def fake_call(method, params=None, timeout=15):
            calls.append((method, params))
            if method == "Target.getTargets":
                return {
                    "targetInfos": [
                        {"type": "page", "targetId": "P"},
                    ]
                }
            if method == "Target.attachToTarget":
                return {"sessionId": "S1"}
            return {}

        bridge.call = fake_call
        asyncio.run(bridge.reattach())
        methods = [m for m, _ in calls]
        self.assertNotIn("Emulation.setTouchEmulationEnabled", methods)
        metrics = [p for m, p in calls if m == "Emulation.setDeviceMetricsOverride"]
        self.assertFalse(metrics[0]["mobile"])

    def test_phone_viewer_gets_mobile_bridge(self):
        # End to end through the websocket handshake: a phone UA on
        # an own session builds a mobile bridge.
        from unittest import mock

        client = TestClient(browser_view.create_app())
        seen = {}

        def make_bridge(session_id, viewport=None, mobile=False):
            seen["viewport"] = viewport
            seen["mobile"] = mobile
            return FakeBridge(session_id, viewport=viewport, mobile=mobile)

        with (
            mock.patch.object(browser_view, "CDPBridge", side_effect=make_bridge),
            mock.patch.object(browser_view, "_grid_live_session", return_value=None),
            mock.patch.object(browser_view, "_grid_session_create", return_value="AAA"),
            mock.patch.object(browser_view, "_grid_session_delete"),
            client.websocket_connect(
                "/ws", headers={"user-agent": self.PHONE_UAS[1]}
            ) as ws,
        ):
            # Receive the attached frame: entering + exiting the
            # socket without reading lets the client close win the
            # race against server-side bridge construction.
            import json as _json

            first = _json.loads(ws.receive_text())
            self.assertEqual(first["kind"], "own")
        self.assertEqual(seen["viewport"], {"width": 390, "height": 844})
        self.assertTrue(seen["mobile"])


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
                def __enter__(self):
                    return io.BytesIO(b'{"value": {"sessionId": "s1"}}')

                def __exit__(self, *a):
                    return False

            return Ctx()

        with mock.patch.object(
            browser_view.urllib.request, "urlopen", side_effect=fake_urlopen
        ):
            browser_view._grid_session_create()
        args = seen["data"]["capabilities"]["alwaysMatch"]["goog:chromeOptions"]["args"]
        self.assertIn("--disable-dev-shm-usage", args)


if __name__ == "__main__":
    unittest.main()
