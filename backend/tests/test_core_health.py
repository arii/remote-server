"""Tests for backend.core system health (subprocess mocked)."""

import json
import unittest
from unittest import mock

from backend.core import get_container_stats, get_system_health


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


STATS_LINES = "\n".join([
    json.dumps({"Name": "remote-buttons-1", "CPUPerc": "0.12%",
                "MemUsage": "12.34MiB / 7.81GiB"}),
    json.dumps({"Name": "remote-terminal-1", "CPUPerc": "2.50%",
                "MemUsage": "1.20GiB / 7.81GiB"}),
])


def _stats_run_factory(compose_out=COMPOSE_PS, stats_out=STATS_LINES,
                       stats_rc=0):
    def fake_run(cmd, **kwargs):
        if "stats" in cmd:
            return FakeProc(stdout=stats_out, returncode=stats_rc)
        return FakeProc(stdout=compose_out)

    return fake_run


class ContainerStatsTest(unittest.TestCase):
    def test_stats_join_state_and_usage(self):
        compose = json.dumps([
            {"Name": "remote-buttons-1", "Service": "buttons",
             "State": "running", "Status": "Up 5 minutes"},
            {"Name": "remote-terminal-1", "Service": "terminal",
             "State": "running", "Status": "Up 2 hours"},
        ])
        body = get_container_stats(
            run=_stats_run_factory(compose_out=compose))
        self.assertEqual(body["containers"], [
            {"name": "remote-buttons-1", "service": "buttons",
             "state": "running", "cpu_pct": 0.12,
             "mem_used": "12.34MiB", "mem_limit": "7.81GiB"},
            {"name": "remote-terminal-1", "service": "terminal",
             "state": "running", "cpu_pct": 2.5,
             "mem_used": "1.20GiB", "mem_limit": "7.81GiB"},
        ])

    def test_missing_stats_row_yields_nulls(self):
        body = get_container_stats(
            run=_stats_run_factory(stats_out=""))
        self.assertEqual(body["containers"], [{
            "name": "remote-buttons-1", "service": "buttons",
            "state": "running", "cpu_pct": None,
            "mem_used": None, "mem_limit": None,
        }])

    def test_stats_failure_keeps_state_rows(self):
        body = get_container_stats(
            run=_stats_run_factory(stats_rc=1, stats_out=""))
        self.assertEqual(len(body["containers"]), 1)
        row = body["containers"][0]
        self.assertEqual(row["state"], "running")
        self.assertIsNone(row["cpu_pct"])

    def test_compose_failure_degrades_to_empty(self):
        body = get_container_stats(
            run=_stats_run_factory(compose_out=""))
        self.assertEqual(body["containers"], [])

    def test_stats_is_one_shot_not_streaming(self):
        seen = []

        def spy(cmd, **kwargs):
            seen.append(cmd)
            return FakeProc(stdout="")

        get_container_stats(run=spy)
        stats_calls = [c for c in seen if "stats" in c]
        self.assertEqual(len(stats_calls), 1)
        self.assertIn("--no-stream", stats_calls[0])


if __name__ == "__main__":
    unittest.main()
