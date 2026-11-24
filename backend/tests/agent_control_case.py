import os
import tempfile
import unittest
from unittest import mock


class AgentControlTestCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(
            mock.patch.dict(
                os.environ,
                {"AGENT_CONTROL_STATE_FILE": os.path.join(directory, "control.json")},
            )
        )

    def test_state_api_response(self):
        from backend.api.routers.agent_control import get_agent_state, write_control_state, AgentControlState
        state = AgentControlState(
            state="RUNNING",
            active_issue=123,
            phase="coding",
            phase_started_at=1000.0,
            last_activity_at=2000.0,
        )
        write_control_state(state)
        response = get_agent_state()
        self.assertIn("phase_duration", response)
        self.assertGreater(response["phase_duration"], 0)
        self.assertIn("last_activity_seconds", response)
        self.assertGreater(response["last_activity_seconds"], 0)
