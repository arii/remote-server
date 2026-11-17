"""Runtime boundary regressions found during the trace audit."""

import copy
import json
import os
import tempfile
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import skill_runs
from backend.api.routers import browser_drive, browser_view
from backend.browser_safety import HumanWheelPaused
from backend.skill_drivers import GridCDPDriver
from backend.tests.test_skill_recorder import FakeBridge
from backend.tests.test_skill_trace_contract import (
    TraceDriver,
    golden_row,
    sample_skill,
)


class TraceRuntimeTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(
            mock.patch.dict(
                os.environ,
                {
                    "BROWSER_RUNS_FILE": str(self.root / "runs.jsonl"),
                    "BROWSER_ACTIVITY_FILE": str(self.root / "activity.jsonl"),
                    "AGENT_CONTROL_STATE_FILE": str(self.root / "control.json"),
                },
            )
        )
        self.enterContext(mock.patch("backend.skill_replay._require_human_not_driving"))
        self.enterContext(mock.patch.object(browser_view, "_maybe_auto_learn"))
        self.enterContext(
            mock.patch.object(
                browser_view,
                "_PAGES_BY_TARGET",
                {
                    "grid": {"page": None, "hash": None},
                    "paired": {"page": None, "hash": None},
                },
            )
        )
        self.enterContext(
            mock.patch.object(browser_view, "_ACTIVITY", deque(maxlen=100))
        )
        self.enterContext(mock.patch.object(browser_view, "_LEARN_TOTAL", [0]))

    def test_cold_live_drive_has_replayable_actual_page_context(self):
        app = FastAPI()
        app.include_router(browser_drive.router)
        with (
            mock.patch.object(browser_drive, "_require_human_not_driving"),
            mock.patch.object(browser_view, "last_page", return_value=(None, None)),
            mock.patch.object(
                browser_view, "_grid_live_session", return_value="session"
            ),
            mock.patch.object(browser_view, "CDPBridge", FakeBridge),
        ):
            response = TestClient(app).post(
                "/api/browser/drive", json={"op": "click", "x": 100, "y": 300}
            )
        self.assertEqual(response.status_code, 200)
        row = skill_runs.list_runs()[0]
        self.assertIsNotNone(row["steps"][0]["page_hash"])
        self.assertTrue(skill_runs.replay_trace(row)["passed"])

    def test_invalid_skill_error_never_persists_validation_input(self):
        secret = "audit-invalid-password-72ad"
        skill = sample_skill(secret, secret)
        del skill["version"]
        driver = TraceDriver(sensitive=True)
        row = skill_runs.run_and_record(skill, driver)
        self.assertFalse(row["passed"])
        self.assertEqual(driver.calls, [])
        self.assertNotIn(secret, (self.root / "runs.jsonl").read_text())
        self.assertEqual(skill_runs.replay_trace(row)["steps"], [])

    def test_dialog_without_cached_page_records_unobservable_hash(self):
        class DialogBridge(FakeBridge):
            async def click(self, x, y):
                self.dialog = {"type": "prompt", "message": "private dialog text"}

        app = FastAPI()
        app.include_router(browser_drive.router)
        with (
            mock.patch.object(browser_drive, "_require_human_not_driving"),
            mock.patch.object(browser_view, "last_page", return_value=(None, None)),
            mock.patch.object(
                browser_view, "_grid_live_session", return_value="session"
            ),
            mock.patch.object(browser_view, "CDPBridge", DialogBridge),
            mock.patch.object(
                browser_drive,
                "_snapshot",
                side_effect=AssertionError("dialog blocks evaluation"),
            ),
        ):
            response = TestClient(app).post("/api/browser/drive", json={"op": "click"})
        self.assertEqual(response.status_code, 200)
        row = skill_runs.list_runs()[0]
        self.assertIsNone(row["steps"][0]["page_hash"])
        self.assertEqual(row["steps"][0]["context"]["page_hash_unavailable"], "dialog")
        self.assertNotIn("private dialog text", json.dumps(row))
        self.assertTrue(skill_runs.replay_trace(row)["passed"])

    def test_append_after_partial_json_keeps_new_run_retrievable(self):
        (self.root / "runs.jsonl").write_text('{"trace_version":')
        row = skill_runs.run_and_record(sample_skill(), TraceDriver())
        self.assertEqual(skill_runs.list_runs(), [row])

    def test_protected_reason_and_distinct_check_expectation_remain_private(self):
        secret = "audit\nquoted'password"
        expected = "distinct-expected-password-a57f"
        row = skill_runs.run_and_record(
            sample_skill(secret, expected),
            TraceDriver(error=repr(secret), sensitive=True),
        )
        with self.subTest(field="escaped driver reason"):
            self.assertNotIn(repr(secret), row["reason"])
        with self.subTest(field="check expectation"):
            self.assertNotIn(expected, json.dumps(row))

    def test_takeover_before_page_read_redacts_unobserved_type_input(self):
        secret = "unobserved-password-d1c7"
        skill = sample_skill(secret, secret)
        skill["steps"] = [dict(skill["steps"][1], n=1)]
        driver = TraceDriver()
        with mock.patch(
            "backend.skill_replay._require_human_not_driving",
            side_effect=HumanWheelPaused(409, "human has the wheel"),
        ):
            row = skill_runs.run_and_record(skill, driver)
        self.assertEqual(driver.calls, [])
        self.assertNotIn(secret, json.dumps(row))
        self.assertEqual(
            skill_runs.replay_trace(row)["steps"][0]["replay_status"],
            "human_intervention",
        )

    def test_activity_and_learn_input_redact_url_credentials(self):
        secret = "audit-url-token-6af9"
        url = f"https://fixture.invalid/form?token={secret}"
        browser_view._log_activity("go", url, url=url)
        self.assertNotIn(secret, (self.root / "activity.jsonl").read_text())
        self.assertNotIn(secret, json.dumps(browser_view._ACTIVITY[-1]))

    def test_short_protected_values_preserve_identifiers_and_event_labels(self):
        skill = sample_skill("a", "aa")
        row = skill_runs.run_and_record(skill, TraceDriver(error="aa", sensitive=True))
        with self.subTest(field="skill identifier"):
            self.assertEqual(skill_runs.list_runs(skill["skill"]), [row])
        with self.subTest(field="redaction marker"):
            self.assertEqual(row["reason"], "[redacted]")
        browser_view._log_activity(
            "go", "https://fixture.invalid/?token=a", via="human", target="paired"
        )
        with self.subTest(field="event labels"):
            event = browser_view._ACTIVITY[-1]
            self.assertEqual(
                (event["kind"], event["via"], event["target"]),
                ("go", "human", "paired"),
            )

    def test_offline_decoder_rejects_incomplete_or_wrongly_typed_metadata(self):
        mutations = (
            lambda row: row.update(trace_version=True),
            lambda row: row["steps"][0].update(n=True),
            lambda row: row.pop("failed_step"),
            lambda row: row["steps"][0]["proposal"].pop("action"),
            lambda row: row["steps"][0]["outcome"].pop("reason"),
            lambda row: row["steps"][0].update(page_hash=None),
        )
        for change in mutations:
            row = copy.deepcopy(golden_row("passed"))
            change(row)
            with self.subTest(row=row), self.assertRaises(ValueError):
                skill_runs.replay_trace(row)

    def test_grid_takeover_at_mutation_is_a_blocked_unexecuted_step(self):
        skill = sample_skill()
        skill["steps"] = skill["steps"][:1]
        driver = GridCDPDriver()
        with (
            mock.patch.object(
                driver,
                "snapshot",
                return_value={
                    "url": skill["source_url"],
                    "title": "Form",
                    "elements": [],
                },
            ),
            mock.patch.object(
                browser_view, "_grid_live_session", return_value="session"
            ),
            mock.patch.object(browser_view, "CDPBridge", FakeBridge),
            mock.patch(
                "backend.api.routers.agent_control.read_control_state",
                return_value=SimpleNamespace(state="PAUSED_BY_USER"),
            ),
        ):
            row = skill_runs.run_and_record(skill, driver)
        self.assertEqual(row["steps"][0]["gate"]["verdict"], "blocked")
        self.assertEqual(row["steps"][0]["outcome"]["verdict"], "blocked")
        self.assertFalse(row["steps"][0]["executed"])
        self.assertEqual(skill_runs.replay_trace(row)["failed_step"], 1)
