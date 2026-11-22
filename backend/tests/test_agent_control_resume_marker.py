"""Pins the resume marker that the frontend BrowserView matches on."""

from backend.api.routers import agent_control
from backend.tests.agent_control_case import AgentControlTestCase


class ResumeMarkerTest(AgentControlTestCase):
    def test_resume_step_keeps_prefix_browser_view_matches(self):
        # frontend/src/views/BrowserView.tsx bannerTextFor skips the
        # "Agent active" banner for steps starting with "Resuming" (#1490).
        state = agent_control.resume_agent()
        self.assertEqual(state["state"], "RUNNING")
        self.assertTrue(state["current_step"].startswith("Resuming"))
