"""Tests for the agent co-browsing pause/resume API (follow_up Issue 11).

Direct function calls with AGENT_CONTROL_STATE_FILE redirected to a
temporary directory; no server or browser needed.
"""
import json
import os
import tempfile
import unittest

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
            "POST /api/agent/handoff/request",
            "POST /api/agent/handoff/resolve",
            "POST /api/agent/pause",
            "POST /api/agent/resume",
        ])


class AgentHandoffApiTest(unittest.TestCase):
    """Agent-initiated handoff (#1097): parked-waiting contract."""

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

    def test_request_parks_with_waiting_state(self):
        from backend.api.routers.agent_control import HandoffRequest
        got = agent_control.request_handoff(
            HandoffRequest(reason="login wall", current_url="https://x/login"))
        self.assertEqual(got["state"], "PAUSED_BY_USER")
        self.assertTrue(agent_control.handoff_pending(
            agent_control.read_control_state()))
        self.assertEqual(got["handoff_reason"], "login wall")
        self.assertEqual(got["handoff_url"], "https://x/login")
        self.assertTrue(got["requires_dom_refresh"])
        self.assertIn("Waiting for human handoff", got["current_step"])

    def test_request_needs_a_reason(self):
        from fastapi import HTTPException
        from backend.api.routers.agent_control import HandoffRequest
        with self.assertRaises(HTTPException) as ctx:
            agent_control.request_handoff(HandoffRequest(reason="  "))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_re_request_refreshes_without_duplicating(self):
        from backend.api.routers.agent_control import HandoffRequest
        first = agent_control.request_handoff(HandoffRequest(reason="mfa"))
        second = agent_control.request_handoff(
            HandoffRequest(reason="captcha"))
        self.assertEqual(second["handoff_reason"], "captcha")
        self.assertGreaterEqual(
            second["handoff_requested_at"], first["handoff_requested_at"])
        with open(self.state_path, encoding="utf-8") as handle:
            stored = json.load(handle)
        self.assertEqual(stored["handoff_reason"], "captcha")

    def test_resolve_clears_and_resumes_with_refresh(self):
        from backend.api.routers.agent_control import (
            HandoffRequest, HandoffResolve)
        agent_control.request_handoff(HandoffRequest(reason="login wall"))
        got = agent_control.resolve_handoff(HandoffResolve())
        self.assertEqual(got["state"], "RUNNING")
        self.assertIsNone(got["handoff_reason"])
        self.assertIsNone(got["handoff_url"])
        self.assertIsNone(got["handoff_requested_at"])
        self.assertTrue(got["requires_dom_refresh"])
        self.assertFalse(agent_control.handoff_pending(
            agent_control.read_control_state()))

    def test_resolve_with_nothing_pending_is_noop_success(self):
        from backend.api.routers.agent_control import HandoffResolve
        got = agent_control.resolve_handoff(HandoffResolve())
        self.assertEqual(got["state"], "RUNNING")
        self.assertIsNone(got["handoff_reason"])


if __name__ == "__main__":
    unittest.main()
