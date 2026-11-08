"""Tests for per-container logs + restart (issue #928)."""

import json
import os
import subprocess
import unittest

from backend.core.containers import get_container_logs, restart_container


def _compose_ps_argv():
    stack = os.environ.get("BACKEND_STACK_DIR",
                           "/home/ari/remote-server")
    return ["docker", "compose", "-f",
            f"{stack}/docker-compose.yml"]


class FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


COMPOSE_PS = json.dumps([
    {"Name": "remote-caddy-1", "Service": "caddy",
     "State": "running", "Status": "Up 51 minutes", "Health": ""},
    {"Name": "remote-terminal-1", "Service": "terminal",
     "State": "running", "Status": "Up 2 hours", "Health": ""},
])


def _docker_run_factory(compose_out=COMPOSE_PS, logs_out="",
                        logs_rc=0, restart_rc=0, restart_err="",
                        seen=None):
    def fake_run(cmd, **kwargs):
        if seen is not None:
            seen.append(cmd)
        if cmd[:2] == ["docker", "logs"]:
            return FakeProc(stdout=logs_out, returncode=logs_rc)
        if cmd[:2] == ["docker", "compose"] and "restart" in cmd:
            return FakeProc(stdout="", stderr=restart_err,
                            returncode=restart_rc)
        return FakeProc(stdout=compose_out)

    return fake_run


class ContainerLogsTest(unittest.TestCase):
    def test_logs_resolve_service_to_container_name(self):
        seen = []
        body = get_container_logs(
            "caddy", tail=100,
            run=_docker_run_factory(logs_out="a\nb\n", seen=seen))
        self.assertEqual(body, {
            "service": "caddy", "name": "remote-caddy-1",
            "lines": ["a", "b"], "truncated": False,
        })
        self.assertEqual(
            seen[1], ["docker", "logs", "--tail", "100",
                      "remote-caddy-1"])

    def test_logs_truncated_when_tail_reached(self):
        out = "\n".join(f"line {i}" for i in range(10))
        body = get_container_logs(
            "caddy", tail=10,
            run=_docker_run_factory(logs_out=out))
        self.assertEqual(len(body["lines"]), 10)
        self.assertTrue(body["truncated"])

    def test_logs_unknown_service(self):
        with self.assertRaises(KeyError):
            get_container_logs(
                "nope", run=_docker_run_factory())

    def test_logs_compose_failure_reads_as_unknown(self):
        with self.assertRaises(KeyError):
            get_container_logs(
                "caddy", run=_docker_run_factory(compose_out=""))

    def test_logs_rejects_malformed_names_without_docker(self):
        seen = []
        run = _docker_run_factory(seen=seen)
        for bad in ("", "../x", "a b", "a/b"):
            with self.assertRaises(ValueError, msg=bad):
                get_container_logs(bad, run=run)
        self.assertEqual(seen, [])

    def test_logs_rejects_bad_tail(self):
        run = _docker_run_factory()
        for bad in (0, -1, 501, "100"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                get_container_logs("caddy", tail=bad, run=run)

    def test_logs_failure_surfaces_tail(self):
        run = _docker_run_factory(
            logs_out="first\nboom", logs_rc=1)
        with self.assertRaises(RuntimeError) as ctx:
            get_container_logs("caddy", run=run)
        self.assertIn("exited 1", str(ctx.exception))
        self.assertIn("boom", str(ctx.exception))

    def test_logs_vanished_container_reads_as_unknown(self):
        run = _docker_run_factory(
            logs_out="Error: No such container: remote-caddy-1",
            logs_rc=1)
        with self.assertRaises(KeyError):
            get_container_logs("caddy", run=run)

    def test_logs_timeout(self):
        def run(cmd, **kwargs):
            if cmd[:2] == ["docker", "logs"]:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=15)
            return FakeProc(stdout=COMPOSE_PS)

        with self.assertRaises(RuntimeError) as ctx:
            get_container_logs("caddy", run=run)
        self.assertIn("timed out", str(ctx.exception))


class RestartContainerTest(unittest.TestCase):
    def test_restart_argv_and_result(self):
        seen = []
        body = restart_container(
            "caddy", run=_docker_run_factory(seen=seen))
        self.assertEqual(body, {"ok": True, "service": "caddy"})
        self.assertEqual(
            seen[1], _compose_ps_argv() + ["restart", "caddy"])

    def test_restart_refuses_terminal_without_docker(self):
        seen = []
        with self.assertRaises(ValueError) as ctx:
            restart_container(
                "terminal", run=_docker_run_factory(seen=seen))
        self.assertIn("terminal", str(ctx.exception))
        self.assertEqual(seen, [])

    def test_restart_unknown_service(self):
        with self.assertRaises(KeyError):
            restart_container(
                "nope", run=_docker_run_factory())

    def test_restart_rejects_malformed_names_without_docker(self):
        seen = []
        run = _docker_run_factory(seen=seen)
        for bad in ("", "../x", "a b"):
            with self.assertRaises(ValueError, msg=bad):
                restart_container(bad, run=run)
        self.assertEqual(seen, [])

    def test_restart_failure_surfaces_tail(self):
        run = _docker_run_factory(restart_rc=1,
                                  restart_err="cannot restart")
        with self.assertRaises(RuntimeError) as ctx:
            restart_container("caddy", run=run)
        self.assertIn("exited 1", str(ctx.exception))
        self.assertIn("cannot restart", str(ctx.exception))

    def test_restart_timeout(self):
        def run(cmd, **kwargs):
            if "restart" in cmd:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=120)
            return FakeProc(stdout=COMPOSE_PS)

        with self.assertRaises(RuntimeError) as ctx:
            restart_container("caddy", run=run)
        self.assertIn("timed out", str(ctx.exception))


try:
    from fastapi.testclient import TestClient

    from backend.api.routers import system as system_router
    from backend.server import app

    HAS_CLIENT = True
except ImportError:
    HAS_CLIENT = False


@unittest.skipUnless(HAS_CLIENT, "fastapi.testclient not importable")
class ContainerRouterTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self._orig_logs = system_router.get_container_logs
        self._orig_restart = system_router.restart_container
        self._orig_ledger = system_router.ledger_append
        self.addCleanup(self._restore)

    def _restore(self):
        system_router.get_container_logs = self._orig_logs
        system_router.restart_container = self._orig_restart
        system_router.ledger_append = self._orig_ledger

    def test_logs_shape(self):
        system_router.get_container_logs = (
            lambda service, tail=100: {
                "service": "caddy", "name": "remote-caddy-1",
                "lines": ["a"], "truncated": False})
        resp = self.client.get("/api/system/containers/caddy/logs")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {
            "service": "caddy", "name": "remote-caddy-1",
            "lines": ["a"], "truncated": False})

    def test_logs_tail_passthrough_and_bounds(self):
        seen = {}

        def fake(service, tail=100):
            seen["tail"] = tail
            return {"service": service, "name": "n",
                    "lines": [], "truncated": False}

        system_router.get_container_logs = fake
        resp = self.client.get(
            "/api/system/containers/caddy/logs", params={"tail": 5})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(seen["tail"], 5)
        for bad in (0, 501):
            resp = self.client.get(
                "/api/system/containers/caddy/logs",
                params={"tail": bad})
            self.assertEqual(resp.status_code, 422, msg=repr(bad))

    def test_logs_error_mapping(self):
        def fake(service, tail=100):
            raise {"-bad": ValueError("bad"),
                   "missing": KeyError("missing"),
                   "docker": RuntimeError("boom")}[service]

        system_router.get_container_logs = fake
        cases = (("-bad", 400), ("missing", 404),
                 ("docker", 500))
        for service, want in cases:
            resp = self.client.get(
                f"/api/system/containers/{service}/logs")
            self.assertEqual(resp.status_code, want, msg=service)

    def test_restart_requires_confirm(self):
        calls = []
        system_router.restart_container = (
            lambda service: calls.append(service) or {"ok": True,
                                                      "service": service})
        for body in ({}, {"intent": "container-restart"},
                     {"intent": "other", "confirmed": True}):
            resp = self.client.post(
                "/api/system/containers/caddy/restart", json=body)
            self.assertEqual(resp.status_code, 400, msg=repr(body))
        self.assertEqual(calls, [])

    def test_restart_shape_and_ledger(self):
        appended = []
        system_router.restart_container = (
            lambda service: {"ok": True, "service": service})
        system_router.ledger_append = appended.append
        resp = self.client.post(
            "/api/system/containers/caddy/restart",
            json={"intent": "container-restart", "confirmed": True})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(),
                         {"ok": True, "service": "caddy"})
        self.assertEqual(len(appended), 1)
        self.assertEqual(appended[0]["kind"], "system")
        self.assertIn("caddy", appended[0]["event"])

    def test_restart_error_mapping(self):
        def fake(service):
            raise {"terminal": ValueError("no terminal"),
                   "missing": KeyError("missing"),
                   "docker": RuntimeError("boom")}[service]

        system_router.restart_container = fake
        cases = (("terminal", 400), ("missing", 404),
                 ("docker", 500))
        for service, want in cases:
            resp = self.client.post(
                f"/api/system/containers/{service}/restart",
                json={"intent": "container-restart",
                      "confirmed": True})
            self.assertEqual(resp.status_code, want, msg=service)


if __name__ == "__main__":
    unittest.main()
