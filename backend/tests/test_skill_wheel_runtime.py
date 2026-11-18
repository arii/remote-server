"""Wheel deltas survive recording, skill storage and driver dispatch."""

import json
import os
import tempfile
import threading
from collections import deque
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from backend import skill_runs, skill_store
from backend.api.routers import browser_view
from backend.skill_drivers import GridCDPDriver
from backend.skill_format import SKILL_SCHEMA_PATH, SkillFile, validate_skill
from backend.skill_replay import run_skill
from backend.tests.agent_control_case import AgentControlTestCase
from backend.tests.test_browser_view import FakeBridge as ViewerBridge
from backend.tests.test_skill_recorder import CANNED_RAW, drive_client


def wheel_skill(event):
    return {
        "skill": "wheel-probe",
        "version": 1,
        "source_url": CANNED_RAW["url"],
        "steps": [
            {
                "n": 1,
                **{key: event[key] for key in ("action", "selector", "dx", "dy")},
                "check": {"type": "element_visible", "expected": ""},
            }
        ],
    }


class WheelRuntimeTest(AgentControlTestCase):
    def setUp(self):
        super().setUp()
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(
            mock.patch.dict(
                os.environ,
                {
                    key: str(self.directory / name)
                    for key, name in (
                        ("BROWSER_ACTIVITY_FILE", "activity.jsonl"),
                        ("BROWSER_RUNS_FILE", "runs.jsonl"),
                        ("BROWSER_SKILLS_DIR", "skills"),
                        ("BROWSER_NOTES_FILE", "notes.md"),
                    )
                },
            )
        )
        for name, value in (
            ("_ACTIVITY", deque(maxlen=100)),
            ("_LEARN_TOTAL", [0]),
            ("_LEARN_STATE", {"covered": 0, "last_learn_at": 0.0}),
            (
                "_LAST_PAGE",
                {
                    "page": {
                        "url": CANNED_RAW["url"],
                        "elements": CANNED_RAW["els"],
                    },
                    "hash": "012345abcdef",
                },
            ),
        ):
            self.enterContext(mock.patch.object(browser_view, name, value))
        self.enterContext(
            mock.patch.object(browser_view, "_grid_live_session", return_value="wheel")
        )
        for method in ("connect", "close"):
            self.enterContext(
                mock.patch.object(
                    browser_view.CDPBridge, method, new_callable=mock.AsyncMock
                )
            )
        self.cdp = self.enterContext(
            mock.patch.object(
                browser_view.CDPBridge, "call", new_callable=mock.AsyncMock
            )
        )
        self.cdp.side_effect = lambda method, *args, **kwargs: (
            {"result": {"value": CANNED_RAW}} if method == "Runtime.evaluate" else {}
        )
        self.schema = Draft202012Validator(json.loads(SKILL_SCHEMA_PATH.read_text()))

    def mouse_events(self):
        return [
            call.args[1]
            for call in self.cdp.call_args_list
            if call.args[0] == "Input.dispatchMouseEvent"
        ]

    def test_recorded_live_wheel_replays_matching_cdp_deltas(self):
        for version, (dx, dy) in enumerate(((17, 240), (-19, -360), (85, 0)), 1):
            with self.subTest(dx=dx, dy=dy):
                self.cdp.reset_mock()
                response = drive_client().post(
                    "/api/browser/drive",
                    json={"op": "wheel", "x": 100, "y": 200, "dx": dx, "dy": dy},
                )
                self.assertEqual(response.status_code, 200, response.text)
                (sent,) = self.mouse_events()
                self.assertEqual((sent["deltaX"], sent["deltaY"]), (dx, dy))
                recorded = skill_runs.list_runs("live-drive")[-1]["steps"][0][
                    "proposal"
                ]
                self.assertEqual((recorded["dx"], recorded["dy"]), (dx, dy))
                skill = wheel_skill(recorded)
                skill["version"] = version
                self.assertEqual(validate_skill(skill), [])
                self.schema.validate(skill)
                normalized = SkillFile.model_validate(skill).model_dump()
                skill_store.save_skill(normalized)
                stored = skill_store.read_skill(skill["skill"])
                self.cdp.reset_mock()
                row = skill_runs.run_and_record(stored, GridCDPDriver())
                self.assertEqual(self.mouse_events(), [sent], row["reason"])
                self.assertTrue(row["passed"], row["reason"])
                durable = skill_runs.list_runs(skill["skill"])[-1]
                offline = skill_runs.replay_trace(durable)
                proposal = offline["steps"][0]["proposal"]
                self.assertEqual((proposal["dx"], proposal["dy"]), (dx, dy))

    def test_drive_activity_records_structured_wheel_deltas(self):
        response = drive_client().post(
            "/api/browser/drive",
            json={"op": "wheel", "x": 100, "y": 200, "dx": -12, "dy": 240},
        )
        self.assertEqual(response.status_code, 200, response.text)
        (event,) = [
            json.loads(line)
            for line in (self.directory / "activity.jsonl").read_text().splitlines()
            if json.loads(line)["kind"] == "drive"
        ]
        self.assertEqual((event.get("dx"), event.get("dy")), (-12, 240))

    def test_human_wheel_records_structured_deltas(self):
        delivered = threading.Event()
        bridge = ViewerBridge("wheel")

        async def wheel(*args):
            delivered.set()

        bridge.wheel = wheel
        with mock.patch.object(browser_view, "CDPBridge", return_value=bridge):
            with TestClient(browser_view.create_app()).websocket_connect("/ws") as ws:
                self.assertEqual(ws.receive_json()["kind"], "agent")
                ws.send_json({"t": "wheel", "x": 100, "y": 200, "dx": 31, "dy": -240})
                self.assertTrue(delivered.wait(2), "wheel did not reach the bridge")
        (event,) = [
            json.loads(line)
            for line in (self.directory / "activity.jsonl").read_text().splitlines()
            if json.loads(line)["kind"] == "wheel"
        ]
        self.assertEqual((event.get("dx"), event.get("dy")), (31, -240))

    def test_missing_deltas_keep_zero_default(self):
        skill = wheel_skill(
            {"action": "wheel", "selector": "[data-testid='name']", "dx": 0, "dy": 0}
        )
        del skill["steps"][0]["dx"]
        del skill["steps"][0]["dy"]
        self.schema.validate(skill)
        result = run_skill(skill, GridCDPDriver())
        self.assertTrue(result["passed"], result["reason"])
        (sent,) = self.mouse_events()
        self.assertEqual((sent["deltaX"], sent["deltaY"]), (0, 0))

    def test_malformed_deltas_rejected_before_driver_dispatch(self):
        for key in ("dx", "dy"):
            for value in (None, True, "120", 1.5, [], {}):
                with self.subTest(key=key, value=value):
                    skill = wheel_skill(
                        {"action": "wheel", "selector": "input", "dx": 0, "dy": 120}
                    )
                    skill["steps"][0][key] = value
                    self.assertTrue(validate_skill(skill))
                    self.assertTrue(list(self.schema.iter_errors(skill)))
                    result = run_skill(skill, GridCDPDriver())
                    self.assertFalse(result["passed"])
                    self.assertEqual(result["steps"], [])
                    self.cdp.assert_not_called()
