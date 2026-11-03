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


class DeployHealthGateTest(unittest.TestCase):
    def test_gate_hits_routable_status_path(self):
        # The gate curls buttons:8000 directly (no Caddy strip), so it
        # must use the root-served /api/status, not /buttons/api/status
        # (which 404s and fails every deploy).
        steps, err = engine.action_steps(
            "deploy-buttons", "remote-server",
            "arii/remote-server", "/srv/remote-server")
        self.assertIsNone(err)
        gate = " ".join(" ".join(s) for s in steps)
        self.assertIn("http://buttons:8000/api/status", gate)
        self.assertNotIn("/buttons/api/status", gate)


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
        self.assertEqual(kwargs["cwd"], engine.stack_dir())

    def test_full_run_flow_mocked(self):
        import backend.api.routers.jobs as jobs_router

        jobs_router.registry = RunRegistry()
        with mock.patch(
            "backend.api.routers.jobs._launch"
        ):
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
        self.assertEqual(resp3.status_code, 400)
        resp3 = self.client.post(
            "/api/jobs/clear",
            json={"intent": "jobs-clear", "confirmed": True})
        self.assertEqual(resp3.json(), {"ok": True, "cleared": 0})
        jobs_router.registry.finish_run(rid, 0)

    def test_run_destructive_requires_confirm(self):
        import backend.api.routers.jobs as jobs_router

        jobs_router.registry = RunRegistry()
        resp = self.client.post("/api/jobs/run", json={"action": "recreate"})
        self.assertEqual(resp.status_code, 400)
        with mock.patch(
            "backend.api.routers.jobs._launch"
        ) as launched:
            resp = self.client.post(
                "/api/jobs/run",
                json={"action": "recreate", "intent": "jobs-run",
                      "confirmed": True})
            self.assertEqual(resp.status_code, 200)
            launched.assert_called_once()
        jobs_router.registry.finish_run(resp.json()["id"], 0)

    def test_run_repo_scoped_validates_repo(self):
        resp = self.client.post(
            "/api/jobs/run",
            json={"action": "preflight", "repo": "no-such-repo"})
        self.assertEqual(resp.status_code, 400)

    def test_run_progress_shapes(self):
        import backend.api.routers.jobs as jobs_router

        jobs_router.registry = RunRegistry()
        rid, err = jobs_router.registry.start_run("logs")
        self.assertIsNone(err)
        jobs_router.registry.append(rid, "hello")
        resp = self.client.get(f"/api/jobs/runs/{rid}")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["done"])
        self.assertIn("hello", body["output"])
        self.assertEqual(body["action"], "logs")
        jobs_router.registry.finish_run(rid, 0)
        resp = self.client.get(f"/api/jobs/runs/{rid}")
        self.assertTrue(resp.json()["done"])
        resp = self.client.get("/api/jobs/runs/nothex")
        self.assertEqual(resp.status_code, 404)
        resp = self.client.get("/api/jobs/runs/aaaaaaaaaaaa")
        self.assertEqual(resp.status_code, 404)

    def test_plans_list_and_read(self):
        import tempfile
        from pathlib import Path

        import backend.api.routers.jobs as jobs_router

        with tempfile.TemporaryDirectory() as tmp:
            plans = Path(tmp) / ".agents" / "plans"
            plans.mkdir(parents=True)
            (plans / "2026-11-03-briefing-plan.md").write_text("# plan\n")
            (plans / "notes.txt").write_text("not a plan")
            with mock.patch.object(jobs_router, "_plans_dir",
                                   return_value=plans):
                resp = self.client.get("/api/jobs/plans",
                                       params={"repo": "remote-server"})
                self.assertEqual(resp.status_code, 200)
                names = [p["name"] for p in resp.json()["plans"]]
                self.assertEqual(names, ["2026-11-03-briefing-plan.md"])
                resp = self.client.get(
                    "/api/jobs/plans/2026-11-03-briefing-plan.md",
                    params={"repo": "remote-server"})
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json()["content"], "# plan\n")
                resp = self.client.get("/api/jobs/plans/missing.md",
                                       params={"repo": "remote-server"})
                self.assertEqual(resp.status_code, 404)

    def test_plans_rejects_traversal_and_bad_repo(self):
        resp = self.client.get("/api/jobs/plans/..%2Fsecret.md",
                               params={"repo": "remote-server"})
        self.assertEqual(resp.status_code, 404)
        resp = self.client.get("/api/jobs/plans/x.md",
                               params={"repo": "no-such-repo"})
        self.assertEqual(resp.status_code, 400)

    def test_sync_agents_cut(self):
        resp = self.client.post(
            "/api/jobs/run", json={"action": "sync-agents"})
        self.assertEqual(resp.status_code, 400)

    def test_compose_env_file_resolution(self):
        import os

        old = os.environ.get("BUTTONS_ENV_FILE")
        os.environ["BUTTONS_ENV_FILE"] = "/tmp/probe.env"
        try:
            argv = engine.compose_argv("ps")
            self.assertIn("--env-file", argv)
            self.assertIn("/tmp/probe.env", argv)
        finally:
            if old is None:
                del os.environ["BUTTONS_ENV_FILE"]
            else:
                os.environ["BUTTONS_ENV_FILE"] = old
        argv = engine.compose_argv("ps")
        if "--env-file" in argv:
            path = argv[argv.index("--env-file") + 1]
            self.assertTrue(os.path.isfile(path))


class ExecuteRunTest(unittest.TestCase):
    def _reg(self, action="logs"):
        reg = RunRegistry()
        rid, err = reg.start_run(action)
        self.assertIsNone(err)
        return reg, rid

    def test_steps_execute_in_order(self):
        reg, rid = self._reg("health")
        seen = []
        engine.execute_run(
            reg, rid, "health", "remote-server", "", "",
            runner=lambda argv: seen.append(argv) or (0, "ok\n"))
        run = reg.runs[rid]
        self.assertTrue(run["done"])
        self.assertEqual(run["exit_code"], 0)
        self.assertEqual(len(seen), 3)
        self.assertIn("$ df", run["output"])
        self.assertIsNone(reg.active_id)

    def test_failing_step_stops_with_note(self):
        reg, rid = self._reg("status")
        engine.execute_run(
            reg, rid, "status", "remote-server", "", "",
            runner=lambda argv: (1, ""))
        run = reg.runs[rid]
        self.assertTrue(run["done"])
        self.assertEqual(run["exit_code"], 1)
        self.assertIn("printed nothing", run["output"])

    def test_run_td_builds_validated_step(self):
        reg, rid = self._reg("run-td")
        seen = []
        engine.execute_run(
            reg, rid, "run-td", "remote-server", "arii/remote-server",
            "/srv/remote-server", td_argv=["td", "context-warm"],
            runner=lambda argv: seen.append(argv) or (0, "ok\n"))
        self.assertEqual(reg.runs[rid]["exit_code"], 0)
        self.assertEqual(len(seen), 1)
        self.assertIn("td context-warm", seen[0][-1])
        reg2, rid2 = self._reg("run-td")
        engine.execute_run(
            reg2, rid2, "run-td", "remote-server", "arii/remote-server",
            "/srv/remote-server", runner=lambda argv: (0, ""))
        self.assertEqual(reg2.runs[rid2]["exit_code"], 1)
        self.assertIn("validated td command",
                      reg2.runs[rid2]["output"])


if __name__ == "__main__":
    unittest.main()
