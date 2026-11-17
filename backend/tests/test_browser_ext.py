"""Paired Chrome protocol and drive API, without a real browser."""

import asyncio
import concurrent.futures
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.api.routers import agent_control, browser_drive, browser_ext, browser_view


class PairedChromeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(
            os.environ,
            {
                "AGENT_CONTROL_STATE_FILE": os.path.join(self.tmp.name, "control.json"),
                "BROWSER_ACTIVITY_FILE": os.path.join(self.tmp.name, "activity.jsonl"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        activity = mock.patch.object(browser_view, "_maybe_auto_learn")
        activity.start()
        self.addCleanup(activity.stop)
        self.addCleanup(browser_view.clear_page, "paired")
        app = FastAPI()
        app.include_router(browser_ext.router)
        app.include_router(browser_drive.router)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.client.headers["origin"] = (
            "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        )
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.pool = concurrent.futures.ThreadPoolExecutor()
        self.addCleanup(self.pool.shutdown)
        self.page = {"url": "https://example.com/", "title": "Paired", "els": []}
        self.commands = []

    def round_trip(
        self, ws, method="GET", path="/snapshot", body=None, sensitive=False
    ):
        future = self.pool.submit(
            self.client.request,
            method,
            "/api/browser" + path + "?target=paired",
            json=body,
        )
        while True:
            command = ws.receive_json()
            self.commands.append(command)
            if command["op"] == "open":
                self.assertEqual(command["url"], "about:blank")
                result = {}
                snapshot = False
            else:
                self.assertEqual(command["op"], "cdp")
                expression = command["params"].get("expression")
                snapshot = expression == browser_drive.SNAPSHOT_JS
                if snapshot:
                    result = {"result": {"value": self.page}}
                elif expression == browser_view.CREDENTIAL_FOCUS_JS:
                    result = {"result": {"value": sensitive}}
                else:
                    result = {"result": {"value": "INPUT"}}
            ws.send_json({"id": command["id"], "result": result})
            if snapshot:
                return future.result(timeout=5)

    def test_snapshot_round_trip_state_and_target_isolation(self):
        grid_page = {"url": "https://grid.test", "title": "Grid", "elements": []}
        with mock.patch.dict(browser_view._LAST_PAGE):
            browser_view.remember_page(grid_page)
            with self.client.websocket_connect("/api/browser/ext") as ws:
                response = self.round_trip(ws)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["kind"], "paired")
                self.assertEqual(response.json()["title"], "Paired")
                self.assertEqual(browser_view.last_page()[0], grid_page)
                self.assertEqual(browser_view.last_page("paired")[0]["title"], "Paired")
                state = self.client.get("/api/browser/state?target=paired").json()
                self.assertEqual(
                    state["paired"],
                    {"connected": True, "url": self.page["url"], "title": "Paired"},
                )
                self.assertEqual(browser_view.recent_activity(1)[0]["target"], "paired")
        self.assertFalse(
            self.client.get("/api/browser/state?target=paired").json()["paired"][
                "connected"
            ]
        )

    def test_empty_snapshot(self):
        self.page = {"url": "about:blank", "title": "", "els": []}
        with self.client.websocket_connect("/api/browser/ext") as ws:
            response = self.round_trip(ws)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["elements"], [])

    def assert_origin_rejected(self, headers):
        with (
            mock.patch.object(browser_view, "_log_activity") as activity,
            mock.patch.object(browser_view, "clear_page") as clear_page,
            self.assertLogs(browser_ext.logger, level="WARNING") as logs,
        ):
            with self.assertRaises(WebSocketDisconnect) as closed:
                with self.client.websocket_connect("/api/browser/ext", headers=headers):
                    self.fail("untrusted handshake accepted")
            self.assertEqual(closed.exception.code, 1008)
            activity.assert_not_called()
            clear_page.assert_not_called()
            self.assertEqual(len(logs.output), 1)
            self.assertIn("missing or non-extension Origin", logs.output[0])

    def test_web_origin_rejected_without_replacing_pair(self):
        with self.client.websocket_connect("/api/browser/ext"):
            bridge = browser_ext.paired_bridge()
            self.assert_origin_rejected({"origin": "https://evil.example"})
            self.assertIs(browser_ext.paired_bridge(), bridge)

    def test_missing_origin_rejected(self):
        self.client.headers.pop("origin")
        self.assert_origin_rejected({})
        self.assertFalse(browser_ext.paired_state()["connected"])

    def test_no_pair_returns_conflict(self):
        for method, path, body in (
            ("GET", "/snapshot", None),
            ("POST", "/drive", {"op": "click"}),
        ):
            response = self.client.request(
                method, "/api/browser" + path + "?target=paired", json=body
            )
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["detail"], "no paired Chrome")

    def test_human_input_pauses_and_refuses_drive(self):
        for kind in ("pointer", "key", "touch"):
            with self.subTest(kind=kind):
                agent_control.resume_agent()
                with self.client.websocket_connect("/api/browser/ext") as ws:
                    ws.send_json({"event": "human_input", "kind": kind})
                    self.round_trip(ws)
                    state = agent_control.read_control_state()
                    self.assertEqual(state.state, "PAUSED_BY_USER")
                    self.assertTrue(state.requires_dom_refresh)
                    response = self.client.post(
                        "/api/browser/drive?target=paired",
                        json={"op": "type", "text": "blocked"},
                    )
                    self.assertEqual(response.status_code, 409)
                    self.assertIn("human is driving", response.json()["detail"])

    def test_replacement_closes_old_connection_and_fails_pending_request(self):
        with self.client.websocket_connect("/api/browser/ext") as old:
            future = self.pool.submit(
                self.client.get, "/api/browser/snapshot?target=paired"
            )
            old.receive_json()
            with self.client.websocket_connect("/api/browser/ext") as new:
                with self.assertRaises(WebSocketDisconnect) as closed:
                    old.receive_json()
                self.assertIn("replaced", closed.exception.reason)
                response = future.result(timeout=5)
                self.assertEqual(response.status_code, 502)
                self.assertIn("replaced", response.text)
                self.assertEqual(self.round_trip(new).status_code, 200)

    def test_timeout_is_loud_and_cleans_pending(self):
        with mock.patch.object(browser_ext, "COMMAND_TIMEOUT", 0.05):
            with self.client.websocket_connect("/api/browser/ext") as ws:
                future = self.pool.submit(
                    self.client.get, "/api/browser/snapshot?target=paired"
                )
                command = ws.receive_json()
                self.assertEqual(command["op"], "open")
                response = future.result(timeout=5)
                self.assertEqual(response.status_code, 502)
                self.assertIn("paired Chrome open", response.text)
                self.assertIn("timed out", response.text)
                self.assertEqual(browser_ext.paired_bridge()._pending, {})

    def test_command_error_and_tab_closed_fail_loudly(self):
        for message in ({"error": "debugger detached"}, {"event": "tab_closed"}):
            with self.subTest(message=message):
                with self.client.websocket_connect("/api/browser/ext") as ws:
                    future = self.pool.submit(
                        self.client.get, "/api/browser/snapshot?target=paired"
                    )
                    command = ws.receive_json()
                    ws.send_json(
                        {"id": command["id"], **message}
                        if "error" in message
                        else message
                    )
                    response = future.result(timeout=5)
                    self.assertEqual(response.status_code, 502)
                    self.assertIn("paired Chrome", response.text)

    def test_type_and_key_redaction_and_plain_text(self):
        for op in ("type", "key"):
            for sensitive in (False, True):
                with self.subTest(op=op, sensitive=sensitive):
                    with self.client.websocket_connect("/api/browser/ext") as ws:
                        self.round_trip(ws)
                        secret = f"secret-probe-{sensitive}" if op == "type" else "s"
                        response = self.round_trip(
                            ws,
                            "POST",
                            "/drive",
                            {"op": op, "text" if op == "type" else "key": secret},
                            sensitive,
                        )
                        self.assertEqual(response.status_code, 200, response.text)
                        expected = "[redacted]" if sensitive else secret
                        event = browser_view.recent_activity(1)[0]
                        self.assertEqual(event["detail"], f"{op} {expected}")
                        self.assertEqual(
                            agent_control.read_control_state().current_step,
                            f"Browser drive: {op} {expected}",
                        )
                        if sensitive and op == "type":
                            self.assertNotIn(
                                secret,
                                Path(os.environ["BROWSER_ACTIVITY_FILE"]).read_text(),
                            )

    def test_snapshot_credential_marker_and_field_kind_redact(self):
        for marker in (
            {"val": "[redacted]"},
            {"kind": "password"},
            {"type": "username"},
        ):
            with self.subTest(marker=marker):
                self.page["els"] = [{"x": 0, "y": 0, "w": 20, "h": 20, **marker}]
                with self.client.websocket_connect("/api/browser/ext") as ws:
                    self.round_trip(ws)
                    response = self.round_trip(
                        ws, "POST", "/drive", {"op": "type", "text": "must-not-log"}
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(
                        browser_view.recent_activity(1)[0]["detail"], "type [redacted]"
                    )

    def test_paired_does_not_fall_back_to_grid(self):
        with mock.patch.object(
            browser_view,
            "_grid_live_session",
            side_effect=AssertionError("Grid queried"),
        ):
            self.assertEqual(
                self.client.get("/api/browser/snapshot?target=paired").status_code, 409
            )
            self.assertEqual(
                self.client.get("/api/browser/state?target=paired").status_code, 200
            )

    def test_paired_click_wheel_and_navigation_use_cdp(self):
        with self.client.websocket_connect("/api/browser/ext") as ws:
            self.round_trip(ws)
            for body, expected in (
                ({"op": "click", "x": 12, "y": 34}, "Input.dispatchMouseEvent"),
                ({"op": "wheel", "dx": 0, "dy": 30}, "Input.dispatchMouseEvent"),
                ({"op": "navigate", "url": "https://example.com/new"}, "Page.navigate"),
            ):
                self.commands.clear()
                response = self.round_trip(ws, "POST", "/drive", body)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(
                    any(command.get("method") == expected for command in self.commands)
                )
                self.assertFalse(
                    any(command["op"] == "open" for command in self.commands)
                )

    def test_cdp_error_does_not_become_empty_result(self):
        with self.client.websocket_connect("/api/browser/ext") as ws:
            self.round_trip(ws)
            future = self.pool.submit(
                self.client.get, "/api/browser/snapshot?target=paired"
            )
            command = ws.receive_json()
            self.assertEqual(command["op"], "cdp")
            ws.send_json({"id": command["id"], "error": "debugger detached"})
            response = future.result(timeout=5)
            self.assertEqual(response.status_code, 502)
            self.assertIn("debugger detached", response.text)

    def test_cdp_navigation_event_updates_only_paired_context(self):
        with self.client.websocket_connect("/api/browser/ext") as ws:
            self.round_trip(ws)
            grid_page = browser_view.last_page()
            ws.send_json(
                {
                    "event": "cdp",
                    "method": "Page.frameNavigated",
                    "params": {"frame": {"id": "main", "url": "https://new.test"}},
                }
            )
            ws.send_json(
                {
                    "event": "cdp",
                    "method": "Page.navigatedWithinDocument",
                    "params": {"frameId": "child", "url": "https://child.test"},
                }
            )
            future = self.pool.submit(
                self.client.get, "/api/browser/snapshot?target=paired"
            )
            command = ws.receive_json()
            self.assertEqual(browser_ext.paired_state()["url"], "https://new.test")
            self.assertEqual(browser_view.last_page("paired")[0]["elements"], [])
            self.assertEqual(browser_view.last_page(), grid_page)
            ws.send_json({"id": command["id"], "error": "probe finished"})
            self.assertEqual(future.result(timeout=5).status_code, 502)

    def test_invalid_target_rejected(self):
        self.assertEqual(
            self.client.get("/api/browser/snapshot?target=unknown").status_code, 422
        )


class BridgeLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_protocol_reply_requires_result_or_error(self):
        """PR 1334 protocol: an id-only reply cannot acknowledge a CDP operation."""
        websocket = mock.Mock()
        bridge = browser_ext.PairedBridge(websocket)

        async def send(message):
            await bridge.receive({"id": message["id"]})

        websocket.send_json = send
        with self.assertRaisesRegex(RuntimeError, "missing result"):
            await bridge.call("Page.enable")

    async def test_contract_A06_retired_events_and_replies_cannot_change_replacement(
        self,
    ):
        """PR 1334 A06: late retired traffic cannot mutate new pair context."""
        old = browser_ext.PairedBridge(mock.Mock())
        new = browser_ext.PairedBridge(mock.Mock())
        new.url, new.title = "https://fresh.test/", "Fresh"
        new.dialog = {"type": "alert", "message": "fresh"}
        pending = asyncio.get_running_loop().create_future()
        new._pending[1] = pending
        page = {"url": new.url, "title": new.title, "elements": []}
        browser_view.remember_page(page, "paired")
        old.disconnect("replaced")
        with mock.patch.object(browser_ext, "_paired", new):
            await old.receive({"id": 1, "result": {"stale": True}})
            await old.receive(
                {
                    "event": "cdp",
                    "method": "Page.frameNavigated",
                    "params": {"frame": {"id": "old", "url": "https://stale.test/"}},
                }
            )
            await old.receive(
                {
                    "event": "cdp",
                    "method": "Page.javascriptDialogOpening",
                    "params": {"type": "prompt", "message": "stale"},
                }
            )
            self.assertEqual(
                browser_ext.paired_state(),
                {"connected": True, "url": "https://fresh.test/", "title": "Fresh"},
            )
            self.assertEqual(new.dialog, {"type": "alert", "message": "fresh"})
            self.assertIs(new._pending[1], pending)
            self.assertFalse(pending.done())
            self.assertEqual(browser_view.last_page("paired")[0], page)
        browser_view.clear_page("paired")
        pending.cancel()

    async def test_contract_A06_inflight_reply_cannot_succeed_after_replacement(self):
        """PR 1334 A06: replacement invalidates even an already-buffered old reply."""
        websocket = mock.Mock()
        old = browser_ext.PairedBridge(websocket)

        async def send(message):
            await old.receive({"id": message["id"], "result": {"stale": True}})
            old.disconnect("replaced")

        websocket.send_json = send
        with self.assertRaises(ConnectionError):
            await old.call("Page.enable")

    async def test_contract_A07_replacement_close_failure_is_observable(self):
        """PR 1334 A07: close failure is logged while the new receiver runs."""
        old = browser_ext.PairedBridge(
            mock.Mock(close=mock.AsyncMock(side_effect=RuntimeError("close failed")))
        )
        socket = mock.Mock(
            headers={"origin": "chrome-extension://buttons"},
            accept=mock.AsyncMock(),
            receive_json=mock.AsyncMock(side_effect=WebSocketDisconnect(1000)),
        )
        with mock.patch.object(browser_ext, "_paired", old):
            with self.assertLogs(browser_ext.logger, level="WARNING"):
                await browser_ext.extension_socket(socket)
        socket.receive_json.assert_awaited_once()

    async def test_contract_A07_stalled_old_close_cannot_block_replacement(self):
        """PR 1334 A07: old-peer cleanup has a deadline independent of peer response."""

        async def stalled_close(**kwargs):
            await asyncio.get_running_loop().create_future()

        old = browser_ext.PairedBridge(mock.Mock(close=stalled_close))
        socket = mock.Mock(
            headers={"origin": "chrome-extension://buttons"},
            accept=mock.AsyncMock(),
            receive_json=mock.AsyncMock(side_effect=WebSocketDisconnect(1000)),
        )
        with (
            mock.patch.object(browser_ext, "_paired", old),
            mock.patch.object(browser_ext, "CLOSE_TIMEOUT", 0, create=True),
        ):
            await asyncio.wait_for(browser_ext.extension_socket(socket), timeout=0.2)
        socket.receive_json.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
