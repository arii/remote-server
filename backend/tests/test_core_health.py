"""Tests for backend.core system health (subprocess mocked)."""

import json
import os
import unittest
from unittest import mock

from backend.core import get_container_stats, get_system_health


def _compose_ps_argv():
    stack = os.environ.get("BACKEND_STACK_DIR",
                           "/home/ari/remote-server")
    return ["docker", "compose", "-f",
            f"{stack}/docker-compose.yml",
            "ps", "--format", "json"]


STATS_ARGV = ["docker", "stats", "--no-stream", "--format",
              "{{json .}}"]


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
        self.assertEqual(run_mock.call_args[0][0],
                         _compose_ps_argv())
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

    def test_compose_exit_failure_marks_error(self):
        body, _ = self._health(
            FakeProc(stderr="boom", returncode=1))
        self.assertEqual(body["containers"], [])
        self.assertIn("exited 1", body["containers_error"])
        self.assertIn("boom", body["containers_error"])
        self.assertIn("total_gb", body["disk"])

    def test_compose_timeout_marks_error(self):
        import subprocess

        with mock.patch("subprocess.run",
                        side_effect=subprocess.TimeoutExpired(
                            cmd="docker", timeout=10)):
            body = get_system_health()
        self.assertEqual(body["containers"], [])
        self.assertIn("timed out", body["containers_error"])

    def test_compose_spawn_failure_marks_error(self):
        with mock.patch("subprocess.run",
                        side_effect=FileNotFoundError("docker")):
            body = get_system_health()
        self.assertEqual(body["containers"], [])
        self.assertIn("failed", body["containers_error"])

    def test_compose_unparseable_marks_error(self):
        body, _ = self._health(FakeProc(stdout="not json at all\n"))
        self.assertEqual(body["containers"], [])
        self.assertIn("unparseable", body["containers_error"])

    def test_empty_listing_has_no_error(self):
        for stdout in ("", "[]"):
            body, _ = self._health(FakeProc(stdout=stdout))
            self.assertEqual(body["containers"], [])
            self.assertIsNone(body["containers_error"])

    def test_happy_path_has_no_error(self):
        body, _ = self._health(FakeProc(stdout=COMPOSE_PS))
        self.assertEqual(len(body["containers"]), 1)
        self.assertIsNone(body["containers_error"])


STATS_LINES = "\n".join([
    json.dumps({"Name": "remote-buttons-1", "CPUPerc": "0.12%",
                "MemUsage": "12.34MiB / 7.81GiB"}),
    json.dumps({"Name": "remote-terminal-1", "CPUPerc": "2.50%",
                "MemUsage": "1.20GiB / 7.81GiB"}),
])


def _stats_run_factory(compose_out=COMPOSE_PS, stats_out=STATS_LINES,
                       stats_rc=0, seen=None):
    def fake_run(cmd, **kwargs):
        if seen is not None:
            seen.append(cmd)
        if "stats" in cmd:
            return FakeProc(stdout=stats_out, returncode=stats_rc)
        return FakeProc(stdout=compose_out)

    return fake_run


class ContainerStatsTest(unittest.TestCase):
    def _check_calls(self, seen):
        self.assertEqual(seen, [_compose_ps_argv(), STATS_ARGV])

    def test_stats_join_state_and_usage(self):
        compose = json.dumps([
            {"Name": "remote-buttons-1", "Service": "buttons",
             "State": "running", "Status": "Up 5 minutes"},
            {"Name": "remote-terminal-1", "Service": "terminal",
             "State": "running", "Status": "Up 2 hours"},
        ])
        seen = []
        body = get_container_stats(
            run=_stats_run_factory(compose_out=compose, seen=seen))
        self._check_calls(seen)
        self.assertEqual(body["containers"], [
            {"name": "remote-buttons-1", "service": "buttons",
             "state": "running", "status": "Up 5 minutes",
             "health": "", "cpu_pct": 0.12,
             "mem_used": "12.34MiB", "mem_limit": "7.81GiB"},
            {"name": "remote-terminal-1", "service": "terminal",
             "state": "running", "status": "Up 2 hours",
             "health": "", "cpu_pct": 2.5,
             "mem_used": "1.20GiB", "mem_limit": "7.81GiB"},
        ])

    def test_missing_stats_row_yields_nulls(self):
        seen = []
        body = get_container_stats(
            run=_stats_run_factory(stats_out="", seen=seen))
        self._check_calls(seen)
        self.assertEqual(body["containers"], [{
            "name": "remote-buttons-1", "service": "buttons",
            "state": "running", "status": "Up 5 minutes",
            "health": "healthy", "cpu_pct": None,
            "mem_used": None, "mem_limit": None,
        }])

    def test_stats_failure_keeps_state_rows(self):
        seen = []
        body = get_container_stats(
            run=_stats_run_factory(stats_rc=1, stats_out="",
                                   seen=seen))
        self._check_calls(seen)
        self.assertEqual(len(body["containers"]), 1)
        row = body["containers"][0]
        self.assertEqual(row["state"], "running")
        self.assertIsNone(row["cpu_pct"])

    def test_compose_failure_degrades_to_empty(self):
        seen = []
        body = get_container_stats(
            run=_stats_run_factory(compose_out="", seen=seen))
        self.assertEqual(body["containers"], [])
        self._check_calls(seen)

    def test_stats_is_one_shot_not_streaming(self):
        seen = []

        def spy(cmd, **kwargs):
            seen.append(cmd)
            return FakeProc(stdout="")

        get_container_stats(run=spy)
        self._check_calls(seen)


if __name__ == "__main__":
    unittest.main()
