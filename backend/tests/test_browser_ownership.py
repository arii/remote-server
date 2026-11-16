"""Human ownership and credential activity boundaries."""

import asyncio
import collections
import contextlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from backend.api.routers import agent_control, browser_view
from backend.skill_drivers import BrowserDriverError, GridCDPDriver
from backend.skill_replay import run_skill
from backend.tests.test_browser_view import FakeBridge
from backend.tests.test_skill_replay import (
    FORM_URL,
    FakeDriver,
    make_good_pages,
    make_three_step_skill,
)


class OwnershipTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.activity = Path(directory.name) / "activity.jsonl"
        patch = mock.patch.dict(
            os.environ,
            {
                "AGENT_CONTROL_STATE_FILE": str(Path(directory.name) / "control.json"),
                "BROWSER_ACTIVITY_FILE": str(self.activity),
            },
        )
        patch.start()
        self.addCleanup(patch.stop)

    @contextlib.contextmanager
    def viewer(self, sensitive=False):
        bridge = FakeBridge("agent")
        self.bridge = bridge
        bridge.credential_focused = mock.AsyncMock(return_value=sensitive)
        with (
            mock.patch.object(browser_view, "CDPBridge", return_value=bridge),
            mock.patch.object(browser_view, "_grid_live_session", return_value="agent"),
            mock.patch.object(
                browser_view, "_pause_agent", side_effect=agent_control.pause_agent
            ) as pause,
            mock.patch.object(browser_view, "_ACTIVITY", collections.deque(maxlen=100)),
            mock.patch.object(browser_view, "_LEARN_TOTAL", [0]),
            mock.patch.object(browser_view, "_maybe_auto_learn"),
            TestClient(browser_view.create_app()).websocket_connect("/ws") as ws,
        ):
            self.wait_view(ws)
            yield ws, bridge, pause

    def wait_view(self, ws):
        while ws.receive_json()["type"] != "view":
            pass

    def send(self, ws, message):
        processed = threading.Event()
        self.bridge.move = mock.AsyncMock(side_effect=lambda *_: processed.set())
        ws.send_json(message)
        ws.send_json({"t": "move", "x": 0, "y": 0})
        self.assertTrue(processed.wait(5), "viewer input was not processed")

    def test_resume_rearms_takeover_on_same_socket(self):
        with self.viewer() as (ws, bridge, pause):
            for _ in range(2):
                agent_control.resume_agent()
                self.send(ws, {"t": "click", "x": 1, "y": 2})
                self.assertEqual(
                    agent_control.read_control_state().state, "PAUSED_BY_USER"
                )
            self.assertEqual(pause.call_count, 2)
            self.send(ws, {"t": "key", "key": "a"})
            self.assertEqual(pause.call_count, 2)
            self.assertIn(("key", "a", ""), bridge.calls)

    def test_touch_phases_pause_after_each_resume(self):
        with self.viewer() as (ws, bridge, pause):
            for phase in ("start", "move", "end"):
                agent_control.resume_agent()
                self.send(ws, {"t": "touch", "phase": phase, "x": 1, "y": 2})
                self.assertEqual(
                    agent_control.read_control_state().state, "PAUSED_BY_USER"
                )
            self.assertEqual(pause.call_count, 3)
            self.send(ws, {"t": "touch", "phase": "invalid", "x": 1, "y": 2})
            self.assertEqual(pause.call_count, 3)
            self.assertEqual(len([c for c in bridge.calls if c[0] == "touch"]), 3)

    def test_credential_activity_is_redacted_but_counted_and_relayed(self):
        with self.viewer(sensitive=True) as (ws, bridge, _):
            self.send(ws, {"t": "key", "key": "q", "code": "KeyQ"})
            self.send(ws, {"t": "type", "text": "credential-probe"})
            events = [
                e
                for e in browser_view.recent_activity(20)
                if e["kind"] in ("key", "type")
            ]
            self.assertEqual(
                [e["detail"] for e in events], ["[redacted]", "[redacted]"]
            )
            self.assertEqual(browser_view._LEARN_TOTAL[0], len(browser_view._ACTIVITY))
            self.assertIn(("key", "q", "KeyQ"), bridge.calls)
            self.assertIn(("type_text", "credential-probe"), bridge.calls)
            stored = self.activity.read_text()
            self.assertNotIn('"detail": "q"', stored)
            self.assertNotIn("credential-probe", stored)

    def test_plain_and_empty_input_activity(self):
        with self.viewer() as (ws, bridge, _):
            self.send(ws, {"t": "key", "key": "a"})
            self.send(ws, {"t": "type", "text": ""})
            events = [
                e
                for e in browser_view.recent_activity(20)
                if e["kind"] in ("key", "type")
            ]
            self.assertEqual([e["detail"] for e in events], ["a"])
            self.assertNotIn(("type_text", ""), bridge.calls)

    def test_replay_refuses_before_reading_page(self):
        agent_control.pause_agent()
        driver = FakeDriver(make_good_pages(), FORM_URL)
        result = run_skill(make_three_step_skill(), driver)
        self.assertFalse(result["passed"])
        self.assertIn("human is driving", result["reason"])
        self.assertEqual(driver.actions, [])

    def test_replay_stops_between_steps_and_resumes(self):
        driver = FakeDriver(make_good_pages(), FORM_URL)
        navigate = driver.navigate

        def takeover(url):
            navigate(url)
            agent_control.pause_agent()

        driver.navigate = takeover
        result = run_skill(make_three_step_skill(), driver)
        self.assertFalse(result["passed"])
        self.assertEqual(result["failed_step"], 2)
        self.assertFalse(any(a.startswith("type_text:") for a in driver.actions))
        agent_control.resume_agent()
        driver = FakeDriver(make_good_pages(), FORM_URL)
        self.assertTrue(run_skill(make_three_step_skill(), driver)["passed"])

    def test_replay_rechecks_after_pre_action_snapshot(self):
        driver = FakeDriver(make_good_pages(), FORM_URL)
        snapshot = driver.snapshot

        def takeover():
            page = snapshot()
            agent_control.pause_agent()
            return page

        driver.snapshot = takeover
        result = run_skill(make_three_step_skill(), driver)
        self.assertFalse(result["passed"])
        self.assertEqual(driver.actions, ["snapshot"])

    def test_grid_refuses_before_attachment(self):
        agent_control.pause_agent()
        driver = GridCDPDriver()
        with mock.patch.object(driver, "_acquire") as acquire:
            with self.assertRaisesRegex(BrowserDriverError, "human is driving"):
                driver.navigate(FORM_URL)
            acquire.assert_not_called()

    def test_grid_rechecks_after_connect(self):
        driver = GridCDPDriver()
        bridge = mock.AsyncMock()
        bridge.connect.side_effect = agent_control.pause_agent
        with (
            mock.patch.object(driver, "_acquire", return_value=("agent", None)),
            mock.patch.object(browser_view, "CDPBridge", return_value=bridge),
        ):
            with self.assertRaisesRegex(BrowserDriverError, "human is driving"):
                driver.navigate(FORM_URL)
            bridge.navigate.assert_not_called()
            bridge.close.assert_awaited_once()

    def test_grid_snapshot_remains_read_only_while_paused(self):
        agent_control.pause_agent()
        driver = GridCDPDriver()
        bridge = mock.AsyncMock()
        bridge.call.return_value = {
            "result": {
                "value": {
                    "url": "about:blank",
                    "title": "",
                    "els": [],
                }
            }
        }
        with (
            mock.patch.object(driver, "_acquire", return_value=("agent", None)),
            mock.patch.object(browser_view, "CDPBridge", return_value=bridge),
        ):
            self.assertEqual(
                driver.snapshot(), {"url": "about:blank", "title": "", "elements": []}
            )


class CredentialFocusTest(unittest.TestCase):
    def test_failed_or_unknown_focus_is_redacted(self):
        for raw in (
            {},
            {"exceptionDetails": {}},
            {"result": {"value": True}},
            {"result": {"value": None}},
        ):
            with self.subTest(raw=raw):
                bridge = browser_view.CDPBridge("agent")
                bridge.ensure_focus = mock.AsyncMock()
                bridge.call = mock.AsyncMock(return_value=raw)
                self.assertTrue(asyncio.run(bridge.credential_focused()))
        bridge.call.side_effect = RuntimeError("page unavailable")
        self.assertTrue(asyncio.run(bridge.credential_focused()))
        bridge.call.side_effect = None
        bridge.call.return_value = {"result": {"value": False}}
        self.assertFalse(asyncio.run(bridge.credential_focused()))

    @unittest.skipUnless(shutil.which("node"), "requires Node.js")
    def test_actual_focus_expression(self):
        script = """
const cases = [
 ['INPUT', true, '', true], ['INPUT', false, '', false],
 ['INPUT', false, 'section-login USERNAME', true],
 ['INPUT', false, 'current-password', true], ['INPUT', false, 'new-password', true],
 ['INPUT', false, 'one-time-code', true], ['INPUT', false, 'username webauthn', true],
 ['INPUT', false, 'email', false], ['IFRAME', false, '', true],
 ['CLOSED-INPUT', false, '', true]
];
for (const [tagName, password, autocomplete, expected] of cases) {
 global.document = {activeElement: {tagName, matches: () => password,
   getAttribute: () => autocomplete}};
 if (eval(EXPRESSION) !== expected) throw new Error(JSON.stringify({tagName, autocomplete}));
}
global.document = {activeElement: null};
if (eval(EXPRESSION) !== false) throw new Error('empty focus');
console.log('credential focus expression: 11 cases passed');
""".replace("EXPRESSION", json.dumps(browser_view.CREDENTIAL_FOCUS_JS))
        result = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, timeout=10
        )
        self.assertEqual(result.returncode, 0, result.stderr)
