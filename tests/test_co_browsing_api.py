"""Tests for the agent co-browsing pause/resume API (follow_up Issue 11).

Direct function calls with AGENT_CONTROL_STATE_FILE redirected to a
temporary directory; no server or browser needed.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.api.routers import agent_control


class AgentControlApiTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_path = os.path.join(tmp.name, "ctl.json")
        self._old = os.environ.get("AGENT_CONTROL_STATE_FILE")
        os.environ["AGENT_CONTROL_STATE_FILE"] = self.state_path
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._old is None:
            os.environ.pop("AGENT_CONTROL_STATE_FILE", None)
        else:
            os.environ["AGENT_CONTROL_STATE_FILE"] = self._old

    def test_initial_state_is_idle(self):
        state = agent_control.get_agent_state()
        self.assertEqual(state["state"], "IDLE")
        self.assertFalse(state["requires_dom_refresh"])

    def test_pause_transitions_and_flags_refresh(self):
        paused = agent_control.pause_agent()
        self.assertEqual(paused["state"], "PAUSED_BY_USER")
        self.assertTrue(paused["requires_dom_refresh"])
        self.assertIn("Paused", paused["current_step"])
        with open(self.state_path, encoding="utf-8") as handle:
            stored = json.load(handle)
        self.assertEqual(stored["state"], "PAUSED_BY_USER")

    def test_resume_transitions_and_flags_refresh(self):
        agent_control.pause_agent()
        resumed = agent_control.resume_agent()
        self.assertEqual(resumed["state"], "RUNNING")
        self.assertTrue(resumed["requires_dom_refresh"])

    def test_corrupt_file_falls_back_to_idle(self):
        with open(self.state_path, "w", encoding="utf-8") as handle:
            handle.write("not-json{{{")
        state = agent_control.get_agent_state()
        self.assertEqual(state["state"], "IDLE")

    def test_routes_registered(self):
        paths = sorted(
            f"{sorted(route.methods)[0]} {route.path}"
            for route in agent_control.router.routes
        )
        self.assertEqual(paths, [
            "GET /api/agent/state",
            "POST /api/agent/pause",
            "POST /api/agent/resume",
        ])


if __name__ == "__main__":
    unittest.main()
