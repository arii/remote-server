"""Tests for backend.core system health (subprocess mocked)."""

import json
import unittest
from unittest import mock

from backend.core import get_system_health


class FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


COMPOSE_PS = json.dumps([
    {"Name": "remote-buttons-1", "Service": "buttons",
     "State": "running", "Status": "Up 5 minutes",
     "Health": "healthy"},
])


class ContainersTest(unittest.TestCase):
    def _health(self, proc):
        with mock.patch("subprocess.run",
                        return_value=proc) as run_mock:
            body = get_system_health()
        return body, run_mock

    def test_compose_env_inherited(self):
        # The compose plugin resolves under the runtime user's HOME;
        # overriding HOME blinds plugin discovery (empty list).
        _, run_mock = self._health(FakeProc(stdout=COMPOSE_PS))
        _, kwargs = run_mock.call_args
        self.assertNotIn("env", kwargs)

    def test_containers_parsed(self):
        body, _ = self._health(FakeProc(stdout=COMPOSE_PS))
        self.assertEqual(body["containers"], [{
            "name": "remote-buttons-1", "service": "buttons",
            "state": "running", "status": "Up 5 minutes",
            "health": "healthy",
        }])

    def test_compose_failure_degrades_to_empty(self):
        body, _ = self._health(
            FakeProc(stderr="boom", returncode=1))
        self.assertEqual(body["containers"], [])
        self.assertIn("total_gb", body["disk"])


if __name__ == "__main__":
    unittest.main()
