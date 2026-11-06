"""Tests for the co-browsing pause gate in deep-agent/pipeline.py.

read_pause_state/clear_refresh_flag against temp state files;
Pipeline._await_if_paused proceeds when running, waits out a
takeover, and times out while still paused.
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

import pipeline as pipeline_mod
from pipeline import Pipeline


def write_state(path, **fields):
    state = {"state": "IDLE", "active_issue": None, "current_step": "",
             "requires_dom_refresh": False, "updated_at": time.time()}
    state.update(fields)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    os.replace(tmp, path)


class PauseStateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "ctl.json")

    def test_missing_file_is_not_paused(self):
        self.assertEqual(
            pipeline_mod.read_pause_state(os.path.join(
                os.path.dirname(self.path), "absent.json")),
            (False, False))

    def test_corrupt_file_is_not_paused(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{{{nope")
        self.assertEqual(pipeline_mod.read_pause_state(self.path),
                         (False, False))

    def test_paused_state_detected(self):
        write_state(self.path, state="PAUSED_BY_USER",
                    requires_dom_refresh=True)
        self.assertEqual(pipeline_mod.read_pause_state(self.path),
                         (True, True))

    def test_running_state_not_paused(self):
        write_state(self.path, state="RUNNING")
        self.assertEqual(pipeline_mod.read_pause_state(self.path),
                         (False, False))

    def test_clear_refresh_flag(self):
        write_state(self.path, state="RUNNING",
                    requires_dom_refresh=True)
        pipeline_mod.clear_refresh_flag(self.path)
        with open(self.path, encoding="utf-8") as handle:
            self.assertFalse(json.load(handle)["requires_dom_refresh"])


class AwaitPausedTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "ctl.json")
        self._old = os.environ.get("AGENT_CONTROL_STATE_FILE")
        os.environ["AGENT_CONTROL_STATE_FILE"] = self.path
        self.addCleanup(self._restore_env)
        self.pipe = Pipeline(repo=tmp.name, intent="probe")

    def _restore_env(self):
        if self._old is None:
            os.environ.pop("AGENT_CONTROL_STATE_FILE", None)
        else:
            os.environ["AGENT_CONTROL_STATE_FILE"] = self._old

    def test_proceeds_when_running(self):
        write_state(self.path, state="RUNNING")
        self.pipe._await_if_paused(poll_s=0.01, timeout_s=5)
        self.assertNotIn("paused",
                         [e["stage"] for e in self.pipe.events])

    def test_waits_out_takeover_then_clears_refresh(self):
        write_state(self.path, state="PAUSED_BY_USER",
                    requires_dom_refresh=True)

        def resume():
            time.sleep(0.2)
            write_state(self.path, state="RUNNING",
                        requires_dom_refresh=True)

        worker = threading.Thread(target=resume)
        worker.start()
        self.pipe._await_if_paused(poll_s=0.01, timeout_s=10)
        worker.join()
        stages = [e["stage"] for e in self.pipe.events]
        self.assertIn("paused", stages)
        self.assertIn("resumed", stages)
        with open(self.path, encoding="utf-8") as handle:
            self.assertFalse(json.load(handle)["requires_dom_refresh"])

    def test_timeout_while_still_paused(self):
        write_state(self.path, state="PAUSED_BY_USER")
        with self.assertRaises(TimeoutError):
            self.pipe._await_if_paused(poll_s=0.01, timeout_s=0.2)


if __name__ == "__main__":
    unittest.main()
