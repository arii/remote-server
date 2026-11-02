"""Tests for the jobs + execution port (stdlib unittest only).

Subprocess is mocked: no real commands run.
"""

import unittest
from unittest import mock

from fastapi.testclient import TestClient

from backend.exec import engine
from backend.exec.engine import RunRegistry


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class ValidateTdCommandTest(unittest.TestCase):
    def test_accept_exact_shapes(self):
        self.assertEqual(
            engine.validate_td_command("td context-warm"), ["td", "context-warm"]
        )
        self.assertEqual(
            engine.validate_td_command("td gh read-pr-reviews --pr 12"),
            ["td", "gh", "read-pr-reviews", "--pr", "12"],
        )
        self.assertEqual(
            engine.validate_td_command("td --json agent fix-ci --pr 3 --execute"),
            ["td", "--json", "agent", "fix-ci", "--pr", "3", "--execute"],
        )

    def test_reject_non_allowlisted(self):
        self.assertIsNone(engine.validate_td_command("td rm -rf /"))
        self.assertIsNone(engine.validate_td_command("td context-warm --force"))
        self.assertIsNone(engine.validate_td_command("td gh read-pr-reviews --pr 0"))
        self.assertIsNone(engine.validate_td_command("td gh read-pr-reviews --pr abc"))
        self.assertIsNone(engine.validate_td_command("td context-warm; reboot"))
        self.assertIsNone(engine.validate_td_command(""))
        self.assertIsNone(engine.validate_td_command("git status"))


class TruncationTest(unittest.TestCase):
    def test_tail_kept(self):
        big = "x" * (engine.MAX_OUTPUT + 100)
        self.assertEqual(engine.truncate_tail(big), big[-engine.MAX_OUTPUT :])
        run = {"output": "", "last_output_at": 0.0}
        engine.append_output(run, big)
        self.assertEqual(run["output"], big[-engine.MAX_OUTPUT :])

    def test_timeout_constants(self):
        self.assertEqual(engine.TD_RUN_TIMEOUT, 120)
        self.assertEqual(engine.PLAN_TIMEOUT, 300)
        self.assertEqual(engine.MAX_OUTPUT, 200_000)


class ClearSemanticsTest(unittest.TestCase):
    def test_clear_terminal_only_by_default(self):
        reg = RunRegistry()
        reg.jobs["a"] = {"id": "a", "state": "terminal", "created_at": 1}
        reg.jobs["b"] = {"id": "b", "state": "active", "created_at": 2}
        self.assertEqual(reg.clear_jobs(), 1)
        self.assertIn("b", reg.jobs)
        self.assertEqual(reg.clear_jobs(all_jobs=True), 1)
        self.assertEqual(reg.jobs, {})

    def test_clear_run_output(self):
        reg = RunRegistry()
        rid, err = reg.start_run("logs")
        self.assertIsNone(err)
        reg.runs[rid]["output"] = "hello"
        got, err = reg.clear_run(rid)
        self.assertEqual(got, rid)
        self.assertEqual(reg.runs[rid]["output"], "")


class RouterTest(unittest.TestCase):
    def setUp(self):
        from backend.server import app

        self.client = TestClient(app)

    def test_run_rejects_unknown_action(self):
        resp = self.client.post("/api/jobs/run", json={"action": "rm-rf"})
        self.assertEqual(resp.status_code, 400)

    def test_run_td_rejects_bad_command(self):
        resp = self.client.post(
            "/api/jobs/run-td", json={"command": "td rm -rf /"}
        )
        self.assertEqual(resp.status_code, 400)

    def test_run_plan_rejects_bad_plan(self):
        resp = self.client.post(
            "/api/jobs/run-plan", json={"plan": "nuke"}
        )
        self.assertEqual(resp.status_code, 400)

    def test_run_plan_mocked(self):
        with mock.patch.object(
            engine, "subprocess"
        ) as _sp, mock.patch(
            "backend.api.routers.jobs.td_run",
            return_value='{"plan_path": "p.md"}',
        ):
            resp = self.client.post(
                "/api/jobs/run-plan", json={"plan": "issue-audit"}
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["plan_path"], "p.md")

    def test_td_run_timeout_and_error(self):
        runner = mock.Mock()
        runner.run.return_value = FakeProc(1, "", "boom failed\nlast line")
        with self.assertRaises(RuntimeError) as ctx:
            engine.td_run("td context-warm", runner=runner)
        self.assertIn("last line", str(ctx.exception))
        args, kwargs = runner.run.call_args
        self.assertEqual(kwargs["timeout"], 120)

    def test_full_run_flow_mocked(self):
        import backend.api.routers.jobs as jobs_router

        jobs_router.registry = RunRegistry()
        resp = self.client.post(
            "/api/jobs/run-td", json={"command": "td context-warm"}
        )
        self.assertEqual(resp.status_code, 200)
        rid = resp.json()["id"]
        # single-flight: second run conflicts
        resp2 = self.client.post("/api/jobs/run", json={"action": "logs"})
        self.assertEqual(resp2.status_code, 409)
        # list + clear jobs
        self.assertEqual(self.client.get("/api/jobs").status_code, 200)
        resp3 = self.client.post("/api/jobs/clear", json={})
        self.assertEqual(resp3.json(), {"ok": True, "cleared": 0})
        jobs_router.registry.finish_run(rid, 0)


if __name__ == "__main__":
    unittest.main()
