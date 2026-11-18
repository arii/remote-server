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
