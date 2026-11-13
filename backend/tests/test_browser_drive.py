"""Tests for the chat-driven browser API (cdp-stream only, no Grid)."""

import os
import tempfile
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routers import agent_control, browser_drive


class FakeBridge:
    def __init__(self, *args, **kwargs):
        self.calls = []
        self.session_id = args[0] if args else kwargs.get("session_id")

    async def connect(self):
        self.calls.append(("connect",))

    async def close(self):
        self.calls.append(("close",))

    async def navigate(self, url):
        self.calls.append(("navigate", url))

    async def reattach(self):
        self.calls.append(("reattach",))

    async def click(self, x, y):
        self.calls.append(("click", x, y))

    async def type_text(self, text):
        self.calls.append(("type", text))

    async def key(self, key, code):
        self.calls.append(("key", key, code))

    async def wheel(self, x, y, dx, dy):
        self.calls.append(("wheel", x, y, dx, dy))

    async def call(self, method, params=None, timeout=15):
        self.calls.append(("call", method))
        if method == "Runtime.evaluate":
            return {
                "result": {
                    "value": {
                        "url": "https://example.com/",
                        "title": "Example",
                        "els": [
                            {
                                "i": 0,
                                "tag": "A",
                                "text": "More",
                                "x": 10,
                                "y": 20,
                                "w": 30,
                                "h": 40,
                            }
                        ],
                    }
                }
            }
        return {}


def make_client():
    app = FastAPI()
    app.include_router(browser_drive.router)
    return TestClient(app, raise_server_exceptions=False)


class DriveApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["AGENT_CONTROL_STATE_FILE"] = os.path.join(
            self.tmp.name, "control.json"
        )
        self.addCleanup(os.environ.pop, "AGENT_CONTROL_STATE_FILE")

    def test_snapshot_prefers_live_agent_session(self):
        with (
            mock.patch.object(
                browser_drive.browser_view,
                "_grid_live_session",
                return_value="agent-sess-1",
            ),
            mock.patch.object(
                browser_drive.browser_view,
                "_viewer_claim",
                side_effect=AssertionError("must not claim when agent live"),
            ),
            mock.patch.object(browser_drive.browser_view, "CDPBridge", FakeBridge),
        ):
            resp = make_client().get("/api/browser/snapshot")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["kind"], "agent")
        self.assertEqual(body["url"], "https://example.com/")
        self.assertEqual(body["elements"][0]["text"], "More")

    def test_snapshot_falls_back_to_viewer_shared_and_releases(self):
        with (
            mock.patch.object(
                browser_drive.browser_view,
                "_grid_live_session",
                return_value=None,
            ),
            mock.patch.object(
                browser_drive.browser_view,
                "_viewer_claim",
                return_value="viewer-sess-2",
            ) as claim,
            mock.patch.object(
                browser_drive.browser_view,
                "_viewer_release",
                return_value=False,
            ) as release,
            mock.patch.object(browser_drive.browser_view, "CDPBridge", FakeBridge),
        ):
            resp = make_client().get("/api/browser/snapshot")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["kind"], "viewer-shared")
        claim.assert_called_once_with()
        release.assert_called_once_with("viewer-sess-2")

    def test_snapshot_busy_when_no_session(self):
        with (
            mock.patch.object(
                browser_drive.browser_view,
                "_grid_live_session",
                return_value=None,
            ),
            mock.patch.object(
                browser_drive.browser_view, "_viewer_claim", return_value=None
            ),
        ):
            resp = make_client().get("/api/browser/snapshot")
        self.assertEqual(resp.status_code, 409)

    def test_drive_refuses_while_human_driving(self):
        paused = agent_control.AgentControlState(state="PAUSED_BY_USER")
        with mock.patch.object(
            agent_control, "read_control_state", return_value=paused
        ):
            resp = make_client().post(
                "/api/browser/drive", json={"op": "click", "x": 1, "y": 2}
            )
        self.assertEqual(resp.status_code, 409)
        self.assertIn("wheel", resp.json()["detail"])

    def test_drive_navigate_rejects_non_http(self):
        with (
            mock.patch.object(
                browser_drive.browser_view,
                "_grid_live_session",
                return_value="agent-sess-1",
            ),
            mock.patch.object(browser_drive.browser_view, "CDPBridge", FakeBridge),
        ):
            resp = make_client().post(
                "/api/browser/drive",
                json={"op": "navigate", "url": "file:///etc/passwd"},
            )
        self.assertEqual(resp.status_code, 400)

    def test_drive_navigate_reattaches_and_snapshots(self):
        seen = {}

        class RecordingBridge(FakeBridge):
            async def navigate(self, url):
                seen["url"] = url
                await super().navigate(url)

        with (
            mock.patch.object(
                browser_drive.browser_view,
                "_grid_live_session",
                return_value="agent-sess-1",
            ),
            mock.patch.object(browser_drive.browser_view, "CDPBridge", RecordingBridge),
        ):
            resp = make_client().post(
                "/api/browser/drive",
                json={"op": "navigate", "url": "https://example.com/"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(seen["url"], "https://example.com/")
        self.assertEqual(resp.json()["url"], "https://example.com/")

    def test_drive_navigate_remembers_last_live_page(self):
        # Agent navigations count toward the restore page: asking
        # the agent to go somewhere must not strand the next fresh
        # session back at example.com.
        prev = browser_drive.browser_view._viewer_last_url
        browser_drive.browser_view._viewer_last_url = None
        self.addCleanup(
            setattr,
            browser_drive.browser_view,
            "_viewer_last_url",
            prev,
        )
        with (
            mock.patch.object(
                browser_drive.browser_view,
                "_grid_live_session",
                return_value="agent-sess-1",
            ),
            mock.patch.object(browser_drive.browser_view, "CDPBridge", FakeBridge),
        ):
            resp = make_client().post(
                "/api/browser/drive",
                json={"op": "navigate", "url": "https://agent-drove.example/w"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            browser_drive.browser_view._viewer_last_url,
            "https://agent-drove.example/w",
        )

    def test_drive_records_chat_presence_step(self):
        with (
            mock.patch.object(
                browser_drive.browser_view,
                "_grid_live_session",
                return_value="agent-sess-1",
            ),
            mock.patch.object(browser_drive.browser_view, "CDPBridge", FakeBridge),
        ):
            resp = make_client().post(
                "/api/browser/drive",
                json={"op": "navigate", "url": "https://example.com/"},
            )
        self.assertEqual(resp.status_code, 200)
        state = agent_control.read_control_state()
        self.assertTrue(
            state.current_step.startswith(browser_drive.CHAT_DRIVE_PREFIX + " navigate")
        )
        self.assertIn("https://example.com/", state.current_step)
        self.assertTrue(state.requires_dom_refresh)
        # Presence only: the drive must not seize the wheel.
        self.assertEqual(state.state, "IDLE")

    def test_activity_returns_human_and_agent_events(self):
        browser_drive.browser_view._log_activity("go", "https://x/", via="human")
        browser_drive.browser_view._log_activity(
            "drive", "navigate https://x/", via="agent"
        )
        resp = make_client().get("/api/browser/activity?limit=10")
        self.assertEqual(resp.status_code, 200)
        events = resp.json()["events"]
        vias = {(e["kind"], e["via"]) for e in events}
        self.assertIn(("go", "human"), vias)
        self.assertIn(("drive", "agent"), vias)

    def test_activity_ring_is_capped(self):
        for i in range(105):
            browser_drive.browser_view._log_activity("click", f"{i},0")
        resp = make_client().get("/api/browser/activity?limit=200")
        self.assertEqual(len(resp.json()["events"]), 100)

    def test_drive_click_passes_coordinates(self):
        bridges = []
        orig_init = FakeBridge.__init__

        def capture_init(self, *a, **k):
            orig_init(self, *a, **k)
            bridges.append(self)

        with (
            mock.patch.object(
                browser_drive.browser_view,
                "_grid_live_session",
                return_value="agent-sess-1",
            ),
            mock.patch.object(FakeBridge, "__init__", capture_init),
            mock.patch.object(browser_drive.browser_view, "CDPBridge", FakeBridge),
        ):
            resp = make_client().post(
                "/api/browser/drive", json={"op": "click", "x": 10, "y": 20}
            )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(("click", 10, 20), bridges[0].calls)
        self.assertIn(("close",), bridges[0].calls)


if __name__ == "__main__":
    unittest.main()
