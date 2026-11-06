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

    def test_unknown_ws_path_closes(self):
        client = TestClient(browser_view.create_app())
        with self.assertRaises(WebSocketDisconnect):
            with client.websocket_connect("/nope"):
                pass

    def test_ws_attempts_grid_session(self):
        # The route is hit and a session is attempted: a dead grid
        # closes the socket instead of hanging the handshake.
        client = TestClient(browser_view.create_app())
        with mock.patch.object(
                browser_view, "_grid_session_create",
                side_effect=RuntimeError("grid down")) as m:
            with self.assertRaises(RuntimeError):
                with client.websocket_connect("/ws") as ws:
                    ws.receive_text()
            m.assert_called_once()


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


if __name__ == "__main__":
    unittest.main()
