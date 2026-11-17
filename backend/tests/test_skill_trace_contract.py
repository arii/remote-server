"""#1263 acceptance: durable redacted steps and browser-free reproduction.

Trace contract: run rows have trace_version=1, context, and steps. Each step
has n/input/proposal/gate/executed/outcome/page_hash/model_version/gate_version/
mode/context. Outcome verdicts distinguish passed, failed, error and blocked;
gate.verdict distinguishes allowed and blocked. Versions are explicit, with
deterministic steps allowed to identify their model as 'deterministic'.

skill_runs.replay_trace(row) takes only a JSON run row. It preserves recorded
verdicts and failure position. Redacted steps additionally have
replay_status='human_intervention'; they never recover hidden values.
"""

import copy
import json
import os
import subprocess
import sys
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
from backend.skill_format import validate_skill
from backend.tests.test_skill_recorder import FakeBridge

URL = "https://fixture.invalid/form"
SECRET = "fixture-SECRET-never-persist-7e3c"


def sample_skill(value="Example", expected="Example"):
    return {
        "skill": "trace-contract-form",
        "version": 3,
        "source_url": URL,
        "steps": [
            {
                "n": 1,
                "action": "navigate",
                "selector": URL,
                "check": {"type": "url_contains", "expected": "/form"},
            },
            {
                "n": 2,
                "action": "type",
                "selector": "[data-testid='field']",
                "value": value,
                "check": {"type": "value_equals", "expected": expected},
            },
            {
                "n": 3,
                "action": "key",
                "selector": "[data-testid='field']",
                "value": "Tab",
                "check": {"type": "url_contains", "expected": "/form"},
            },
        ],
    }


class TraceDriver:
    def __init__(self, error="", sensitive=False):
        self.calls = []
        self.value = ""
        self.error = error
        self.sensitive = sensitive

    def snapshot(self):
        self.calls.append("snapshot")
        return {
            "url": URL,
            "title": "Synthetic trace page",
            "elements": [
                {
                    "tag": "INPUT",
                    "tid": "field",
                    "type": "password" if self.sensitive else "text",
                    "val": "[redacted]" if self.sensitive else self.value,
                    "text": "[redacted]" if self.sensitive else self.value,
                    "x": 20,
                    "y": 20,
                }
            ],
        }

    def navigate(self, url):
        self.calls.append("navigate")

    def type_text(self, value):
        self.calls.append("type")
        if self.error:
            raise RuntimeError(self.error)
        self.value = value

    def key(self, key):
        self.calls.append("key")

    def click(self, x, y):
        self.calls.append("click")

    def wheel(self, x, y, dx, dy):
        self.calls.append("wheel")


def golden_row(verdict):
    steps = []
    for n, action in ((1, "navigate"), (2, "type")):
        final = n == 2
        outcome = verdict if final else "passed"
        blocked = outcome == "blocked"
        steps.append(
            {
                "n": n,
                "input": {"value": "Example" if final else URL},
                "proposal": {
                    "action": action,
                    "selector": "[data-testid='field']" if final else URL,
                    "value": "Example" if final else "",
                },
                "gate": {
                    "verdict": "blocked" if blocked else "allowed",
                    "reason": "human has the wheel" if blocked else "",
                },
                "executed": not blocked,
                "outcome": {
                    "verdict": outcome,
                    "reason": "recorded failure"
                    if final and verdict != "passed"
                    else "",
                },
                "page_hash": "012345abcdef",
                "model_version": "deterministic",
                "gate_version": "fixture-gate-v1",
                "mode": "replay",
                "context": {"url": URL},
            }
        )
    passed = verdict == "passed"
    return {
        "trace_version": 1,
        "run_number": 1,
        "skill": "unavailable-skill",
        "version": 9,
        "context": {"source_url": URL, "mode": "replay"},
        "passed": passed,
        "failed_step": None if passed else 2,
        "reason": "" if passed else "recorded failure",
        "steps": steps,
    }


class SkillTraceContractTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.log = self.root / "runs.jsonl"
        self.enterContext(
            mock.patch.dict(
                os.environ,
                {
                    "BROWSER_RUNS_FILE": str(self.log),
                    "BROWSER_DATA_DIR": str(self.root),
                    "BROWSER_ACTIVITY_FILE": str(self.root / "activity.jsonl"),
                    "BROWSER_NOTES_FILE": str(self.root / "notes.md"),
                    "AGENT_CONTROL_STATE_FILE": str(self.root / "control.json"),
                },
            )
        )
        self.control = self.enterContext(
            mock.patch(
                "backend.api.routers.browser_drive.agent_control.read_control_state",
                return_value=SimpleNamespace(state="RUNNING"),
            )
        )

    def recorded(self, skill, driver, *, valid=True):
        self.assertEqual(
            bool(validate_skill(skill)), not valid, "invalid acceptance setup"
        )
        result = skill_runs.run_and_record(skill, driver)
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["passed"], result["passed"])
        self.assertEqual(row["failed_step"], result["failed_step"])
        return row

    def assert_trace(self, row, count, mode="replay"):
        self.assertIn(
            "steps", row, "#1263 durable run rows must retain the complete step trace"
        )
        self.assertEqual(row["trace_version"], 1)
        self.assertIsInstance(row["context"], dict)
        self.assertEqual(len(row["steps"]), count)
        for n, step in enumerate(row["steps"], 1):
            self.assertEqual(step["n"], n)
            for field in (
                "input",
                "proposal",
                "gate",
                "executed",
                "outcome",
                "page_hash",
                "model_version",
                "gate_version",
                "mode",
                "context",
            ):
                self.assertIn(field, step)
            self.assertIsInstance(step["executed"], bool)
            self.assertIn(step["gate"]["verdict"], ("allowed", "blocked"))
            self.assertIn(
                step["outcome"]["verdict"], ("passed", "failed", "error", "blocked")
            )
            self.assertEqual(step["mode"], mode)
            for field in ("model_version", "gate_version"):
                self.assertIsInstance(step[field], str)
                self.assertTrue(step[field].strip())
                self.assertNotEqual(step[field].lower(), "unknown")
            self.assertIsInstance(step["context"], dict)
            if step["executed"]:
                self.assertRegex(step["page_hash"], r"^[a-f0-9]{12,64}$")

    def offline(self, row):
        replay = getattr(skill_runs, "replay_trace", None)
        self.assertTrue(
            callable(replay), "#1263 requires browser-free skill_runs.replay_trace(row)"
        )
        with (
            mock.patch(
                "socket.socket",
                side_effect=AssertionError("offline trace opened a socket"),
            ),
            mock.patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("offline trace used HTTP"),
            ),
            mock.patch(
                "backend.skill_drivers.PlaywrightDriver._ensure",
                side_effect=AssertionError("offline trace used Chromium"),
            ),
            mock.patch(
                "backend.skill_drivers.GridCDPDriver._acquire",
                side_effect=AssertionError("offline trace used Grid"),
            ),
            mock.patch(
                "backend.skill_store.read_skill",
                side_effect=AssertionError("offline trace loaded a skill"),
            ),
        ):
            result = replay(copy.deepcopy(row))
        self.assertEqual(result["passed"], row["passed"])
        self.assertEqual(result["failed_step"], row["failed_step"])
        self.assertEqual(result["reason"], row["reason"])
        self.assertEqual(
            [step["n"] for step in result["steps"]],
            [step["n"] for step in row["steps"]],
        )
        for actual, recorded in zip(result["steps"], row["steps"]):
            self.assertEqual(actual["proposal"], recorded["proposal"])
            self.assertEqual(actual["gate"], recorded["gate"])
            self.assertEqual(actual["outcome"], recorded["outcome"])
            self.assertEqual(actual["executed"], recorded["executed"])
            for field in (
                "mode",
                "model_version",
                "gate_version",
                "page_hash",
                "context",
            ):
                self.assertEqual(actual[field], recorded[field])
        return result

    def test_live_drive_persists_a_live_step_not_only_an_activity_message(self):
        """#1263: the drive API durably records live proposals and gate outcomes."""
        app = FastAPI()
        app.include_router(browser_drive.router)
        page = TraceDriver().snapshot()
        with (
            mock.patch.object(
                browser_view, "_grid_live_session", return_value="fixture-session"
            ),
            mock.patch.object(browser_view, "CDPBridge", FakeBridge),
            mock.patch.object(
                browser_view,
                "_PAGES_BY_TARGET",
                {"grid": {"page": page, "hash": "012345abcdef"}},
            ),
            mock.patch.object(browser_view, "_ACTIVITY", deque(maxlen=500)),
            mock.patch.object(browser_view, "_LEARN_TOTAL", [0]),
            mock.patch(
                "backend.api.routers.browser_drive.agent_control.write_control_state"
            ),
            TestClient(app) as client,
        ):
            response = client.post(
                "/api/browser/drive", json={"op": "click", "x": 20, "y": 20}
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(
            self.log.exists(), "#1263 live drive must write a durable run trace"
        )
        row = json.loads(self.log.read_text().splitlines()[-1])
        self.assert_trace(row, 1, mode="live")
        self.assertEqual(row["steps"][0]["proposal"]["action"], "click")
        self.assertEqual(row["steps"][0]["gate"]["verdict"], "allowed")
        self.assertTrue(row["steps"][0]["executed"])
        self.offline(row)

    def test_success_retains_every_proposal_gate_outcome_and_version(self):
        """#1263: durable rows, not just in-memory results, retain all step context."""
        row = self.recorded(sample_skill(), TraceDriver())
        self.assertTrue(row["passed"])
        self.assert_trace(row, 3)
        self.assertEqual(
            [step["proposal"]["action"] for step in row["steps"]],
            ["navigate", "type", "key"],
        )
        self.assertEqual(row["steps"][1]["proposal"]["value"], "Example")
        self.assertEqual(row["steps"][1]["input"]["value"], "Example")
        self.assertTrue(
            all(step["outcome"]["verdict"] == "passed" for step in row["steps"])
        )
        self.offline(row)

    def test_driver_error_reproduces_the_failure_point_after_restart(self):
        """#1263: the saved row alone reproduces an error without browser or skill files."""
        driver = TraceDriver(error="fixture driver disconnected")
        row = self.recorded(sample_skill(), driver)
        self.assertFalse(row["passed"])
        self.assertEqual(row["failed_step"], 2)
        self.assertNotIn("key", driver.calls)
        self.assert_trace(row, 2)
        self.assertEqual(row["steps"][1]["outcome"]["verdict"], "error")
        self.offline(row)
        self.log.unlink()
        source = """
import json, socket, sys
from unittest import mock
from backend import skill_runs
row = json.load(sys.stdin)
with mock.patch.object(socket, 'socket', side_effect=AssertionError('offline network')), mock.patch('backend.skill_store.read_skill', side_effect=AssertionError('offline skill lookup')):
    print(json.dumps(skill_runs.replay_trace(row)))
"""
        child = subprocess.run(
            [sys.executable, "-c", source],
            check=False,
            input=json.dumps(row),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(child.returncode, 0, child.stderr)
        restored = json.loads(child.stdout)
        self.assertFalse(restored["passed"])
        self.assertEqual(restored["failed_step"], 2)
        self.assertEqual(
            [step["proposal"]["action"] for step in restored["steps"]],
            ["navigate", "type"],
        )
        self.assertEqual(restored["steps"][1]["outcome"], row["steps"][1]["outcome"])

    def test_gate_block_is_distinct_from_a_driver_error(self):
        """#1263: blocked proposals persist their gate verdict without executing."""
        self.control.return_value = SimpleNamespace(state="PAUSED_BY_USER")
        driver = TraceDriver()
        row = self.recorded(sample_skill(), driver)
        self.assertFalse(row["passed"])
        self.assertEqual(row["failed_step"], 1)
        self.assertEqual(driver.calls, [])
        self.assert_trace(row, 1)
        step = row["steps"][0]
        self.assertEqual(step["gate"]["verdict"], "blocked")
        self.assertEqual(step["outcome"]["verdict"], "blocked")
        self.assertFalse(step["executed"])
        self.assertTrue(step["gate"]["reason"])
        self.offline(row)

    def test_failed_check_retains_executed_action_and_failed_verdict(self):
        """#1263: a failed deterministic check is not rewritten as an execution error."""
        row = self.recorded(sample_skill(expected="A different value"), TraceDriver())
        self.assertEqual(row["failed_step"], 2)
        self.assert_trace(row, 2)
        step = row["steps"][1]
        self.assertTrue(step["executed"])
        self.assertEqual(step["gate"]["verdict"], "allowed")
        self.assertEqual(step["outcome"]["verdict"], "failed")
        self.offline(row)

    def test_redacted_input_stays_redacted_in_proposals_checks_and_errors(self):
        """#1263: secrets never reach durable JSON; offline steps require a human."""
        row = self.recorded(
            sample_skill(value=SECRET, expected=SECRET),
            TraceDriver(error=f"cannot type {SECRET}", sensitive=True),
        )
        self.assertNotIn(
            SECRET, self.log.read_text(), "secret leaked through a durable trace field"
        )
        self.assert_trace(row, 2)
        self.assertEqual(row["steps"][1]["proposal"]["value"], "[redacted]")
        self.assertEqual(row["steps"][1]["input"]["value"], "[redacted]")
        restored = self.offline(row)
        self.assertEqual(restored["steps"][1]["replay_status"], "human_intervention")
        self.assertNotIn(SECRET, json.dumps(restored))

    def test_invalid_empty_skill_has_an_empty_reproducible_trace(self):
        """#1263: validation failure records no invented or executed steps."""
        skill = sample_skill()
        skill["steps"] = []
        driver = TraceDriver()
        row = self.recorded(skill, driver, valid=False)
        self.assertFalse(row["passed"])
        self.assertIsNone(row["failed_step"])
        self.assertEqual(driver.calls, [])
        self.assert_trace(row, 0)
        self.offline(row)

    def test_independently_authored_traces_preserve_success_error_and_gate_block(self):
        """#1263: offline reproduction is not a producer/consumer self-consistency test."""
        for verdict in ("passed", "error", "blocked"):
            for mode in ("live", "replay"):
                with self.subTest(verdict=verdict, mode=mode):
                    row = golden_row(verdict)
                    row["context"]["mode"] = mode
                    for step in row["steps"]:
                        step["mode"] = mode
                    self.offline(row)

    def test_incomplete_trace_cannot_be_presented_as_a_successful_reproduction(self):
        """#1263: absent steps or a missing failure step fail closed."""
        replay = getattr(skill_runs, "replay_trace", None)
        self.assertTrue(
            callable(replay), "#1263 requires browser-free skill_runs.replay_trace(row)"
        )
        missing_failure = golden_row("error")
        missing_failure["steps"] = missing_failure["steps"][:1]
        contradictory = golden_row("error")
        contradictory.update(passed=True, failed_step=None)
        for invalid in ({}, missing_failure, contradictory):
            with self.subTest(row=invalid), self.assertRaises(ValueError):
                replay(invalid)
