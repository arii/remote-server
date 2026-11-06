"""Tests for the jobs + execution port (stdlib unittest only).

Subprocess is mocked except for the watchdog tests, which must run a
real hung process (a short python sleep) to prove the kill.
"""

import asyncio
import inspect
import os
import signal
import sys
import time
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

    def test_reject_non_allowlisted(self):
        # #428: fix-ci dispatches to a dead backend, cut from the allowlist.
        self.assertIsNone(
            engine.validate_td_command(
                "td --json agent fix-ci --pr 3 --execute"
            )
        )
        self.assertIsNone(engine.validate_td_command("td rm -rf /"))
        self.assertIsNone(engine.validate_td_command("td context-warm --force"))
        self.assertIsNone(engine.validate_td_command("td gh read-pr-reviews --pr 0"))
        self.assertIsNone(engine.validate_td_command("td gh read-pr-reviews --pr abc"))
        self.assertIsNone(engine.validate_td_command("td context-warm; reboot"))
        self.assertIsNone(engine.validate_td_command(""))
        self.assertIsNone(engine.validate_td_command("git status"))

    def test_accept_diagnostic_shapes(self):
        self.assertEqual(engine.validate_td_command("td doctor"), ["td", "doctor"])
        self.assertEqual(
            engine.validate_td_command("td preflight"), ["td", "preflight"]
        )
        self.assertEqual(
            engine.validate_td_command("td gh audit-pr --pr 12 --fetch --audit"),
            ["td", "gh", "audit-pr", "--pr", "12", "--fetch", "--audit"],
        )
        self.assertEqual(
            engine.validate_td_command("td ai review 12"),
            ["td", "ai", "review", "12"],
        )

    def test_reject_audit_submit_and_bare_audit(self):
        # --submit posts a review: never one-tap dispatchable.
        self.assertIsNone(
            engine.validate_td_command("td gh audit-pr --pr 12 --fetch --audit --submit")
        )
        # Bare audit-pr with no stage flags is a server-side no-op.
        self.assertIsNone(engine.validate_td_command("td gh audit-pr --pr 12"))


class ValidatePlanArgvTest(unittest.TestCase):
    def test_accept_builder_shapes(self):
        # Everything build_plan_argv produces must validate: the
        # run-plan step execs the attached argv only when it does.
        cases = [
            ("issue-audit", "", [], ""),
            ("workflow-audit", "", [], ""),
            ("review", "12", [], ""),
            ("aggregation", "", ["3", "7"], "rollup/x-1.md"),
        ]
        for plan, pr, prs, target in cases:
            argv, err = engine.build_plan_argv(plan, pr, prs, target)
            self.assertIsNone(err)
            self.assertTrue(engine.validate_plan_argv(argv), argv)

    def test_reject_non_plan_argv(self):
        bad = [
            None,
            [],
            ["td", "--json", "agent"],
            ["td", "--json", "agent", "plan-issue-audit"],
            ["td", "--json", "agent", "plan-issue-audit",
             "--all-open", "--extra"],
            ["td", "--json", "agent", "plan-workflow-audit",
             "--all-open"],
            ["td", "--json", "agent", "plan-review", "--pr", "0"],
            ["td", "--json", "agent", "plan-review", "--pr", "abc"],
            ["td", "--json", "agent", "plan-review", "--pr", "3",
             "--execute"],
            ["td", "--json", "agent", "plan-aggregation",
             "--target", "x.md"],
            ["td", "--json", "agent", "plan-aggregation",
             "--pr", "3", "--target", "no spaces"],
            ["td", "--json", "agent", "plan-aggregation",
             "--pr", "0", "--target", "x.md"],
            ["td", "--json", "agent", "plan-nuke", "--all-open"],
            ["td", "context-warm"],
            ["td", "--json", "agent", "plan-review", "--pr", "3; reboot"],
            "td --json agent plan-issue-audit --all-open",
        ]
        for argv in bad:
            self.assertFalse(engine.validate_plan_argv(argv), argv)
        many = ["td", "--json", "agent", "plan-aggregation"]
        for n in range(1, 12):
            many += ["--pr", str(n)]
        many += ["--target", "x.md"]
        self.assertFalse(engine.validate_plan_argv(many))


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
        done_id, err = reg.start_run("logs")
        self.assertIsNone(err)
        reg.finish_run(done_id, 0)
        active_id, err = reg.start_run("logs")
        self.assertIsNone(err)
        # Same store the list reads: finished runs go, active stays.
        self.assertEqual(reg.clear_jobs(), (1, None))
        self.assertNotIn(done_id, reg.runs)
        self.assertIn(active_id, reg.runs)
        # Clear-all refuses while a run is active (#512): the
        # active record stays visible and keeps the slot.
        count, err = reg.clear_jobs(all_jobs=True)
        self.assertIsNone(count)
        self.assertIn("still active", err)
        self.assertIn(active_id, err)
        self.assertIn(active_id, reg.runs)
        self.assertEqual(reg.active_ids, {active_id})
        # Once the run finishes, clear-all goes through.
        reg.finish_run(active_id, 0)
        self.assertEqual(reg.clear_jobs(all_jobs=True), (1, None))
        self.assertEqual(reg.runs, {})

    def test_clear_all_refusal_names_visible_run(self):
        # Regression (#512): a refused clear-all deletes nothing,
        # bumps nothing, and the 409 names a run that still lists.
        reg = RunRegistry()
        done_id, _ = reg.start_run("logs")
        reg.finish_run(done_id, 0)
        active_id, _ = reg.start_run("logs")
        before = reg._version
        count, err = reg.clear_jobs(all_jobs=True)
        self.assertIsNone(count)
        self.assertIn(active_id, err)
        self.assertIn(done_id, reg.runs)
        self.assertIn(active_id, reg.runs)
        self.assertEqual(reg._version, before)

    def test_clear_run_output(self):
        reg = RunRegistry()
        rid, err = reg.start_run("logs")
        self.assertIsNone(err)
        reg.runs[rid]["output"] = "hello"
        got, err = reg.clear_run(rid)
        self.assertEqual(got, rid)
        self.assertEqual(reg.runs[rid]["output"], "")

    def test_second_start_queues_behind_full_pool(self):
        # Issue #1025: capacity is a FIFO queue, not single-flight.
        # The (MAX+1)-th dispatch waits with a place-in-line instead
        # of failing; finishing the head promotes it.
        reg = RunRegistry()
        import backend.exec.engine as engine_mod
        cap = engine_mod.MAX_CONCURRENT_RUNS
        ids = []
        for _ in range(cap):
            rid, err = reg.start_run("logs")
            self.assertIsNone(err)
            ids.append(rid)
        rid2, err2 = reg.start_run("logs")
        self.assertIsNone(err2)
        self.assertTrue(reg.runs[rid2]["queued"])
        self.assertEqual(reg.queue_position(rid2), 1)
        self.assertEqual(reg.active_ids, set(ids))
        # Head finishes: the waiter promotes into the freed slot.
        reg.finish_run(ids[0], 0)
        self.assertIn(rid2, reg.active_ids)
        self.assertFalse(reg.runs[rid2]["queued"])
        self.assertEqual(reg.queue_position(rid2), 0)


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

    def test_run_plan_starts_first_class_run(self):
        # #452: run-plan takes a run slot, appears in the
        # job list with progress, and launches through the standard
        # run path — no inline 5-minute await.
        import backend.api.routers.jobs as jobs_router

        jobs_router.registry = RunRegistry()
        with mock.patch(
            "backend.api.routers.jobs._launch"
        ) as launched:
            resp = self.client.post(
                "/api/jobs/run-plan", json={"plan": "issue-audit"}
            )
            self.assertEqual(resp.status_code, 200)
            rid = resp.json()["id"]
            launched.assert_called_once_with(
                rid, "run-plan", "remote-server",
                td_argv=["td", "--json", "agent",
                         "plan-issue-audit", "--all-open"])
            rows = self.client.get("/api/jobs").json()["jobs"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], rid)
            self.assertEqual(rows[0]["kind"], "run-plan")
            self.assertFalse(rows[0]["done"])
            prog = self.client.get(f"/api/jobs/runs/{rid}").json()
            self.assertFalse(prog["done"])
            self.assertEqual(prog["action"], "run-plan")
            # Queue, not single-flight (#1025): a second plan takes
            # the second slot, and an action run arriving with both
            # slots full waits queued with its FIFO position.
            resp2 = self.client.post(
                "/api/jobs/run-plan",
                json={"plan": "review", "pr": "3"},
            )
            self.assertEqual(resp2.status_code, 200)
            body2 = resp2.json()
            self.assertFalse(body2["queued"])
            self.assertEqual(body2["position"], 0)
            resp3 = self.client.post(
                "/api/jobs/run", json={"action": "logs"})
            self.assertEqual(resp3.status_code, 200)
            body3 = resp3.json()
            self.assertTrue(body3["queued"])
            self.assertEqual(body3["position"], 1)
        for done_id in (rid, body2["id"], body3["id"]):
            jobs_router.registry.finish_run(done_id, 0)
        jobs_router.registry = RunRegistry()

    def test_run_plan_queues_behind_active_action_runs(self):
        # Queue (#1025): with both slots held by action runs, a
        # plan waits queued with its FIFO position instead of
        # conflicting.
        import backend.api.routers.jobs as jobs_router

        jobs_router.registry = RunRegistry()
        with mock.patch("backend.api.routers.jobs._launch"):
            resp = self.client.post(
                "/api/jobs/run", json={"action": "logs"})
            self.assertEqual(resp.status_code, 200)
            resp_b = self.client.post(
                "/api/jobs/run", json={"action": "logs"})
            self.assertEqual(resp_b.status_code, 200)
            self.assertFalse(resp_b.json()["queued"])
            resp2 = self.client.post(
                "/api/jobs/run-plan", json={"plan": "issue-audit"})
            self.assertEqual(resp2.status_code, 200)
            body2 = resp2.json()
            self.assertTrue(body2["queued"])
            self.assertEqual(body2["position"], 1)
        for done_id in (resp.json()["id"], resp_b.json()["id"],
                        body2["id"]):
            jobs_router.registry.finish_run(done_id, 0)
        jobs_router.registry = RunRegistry()

    def test_td_run_timeout_and_error(self):
        runner = mock.Mock()
        runner.run.return_value = FakeProc(1, "", "boom failed\nlast line")
        with self.assertRaises(RuntimeError) as ctx:
            engine.td_run("td context-warm", runner=runner)
        self.assertIn("last line", str(ctx.exception))
        args, kwargs = runner.run.call_args
        self.assertEqual(args[0], ["bash", "-c", "td context-warm"])
        self.assertEqual(kwargs["timeout"], 120)
        self.assertEqual(kwargs["cwd"], engine.stack_dir())

    def test_full_run_flow_mocked(self):
        import backend.api.routers.jobs as jobs_router

        jobs_router.registry = RunRegistry()
        with mock.patch(
            "backend.api.routers.jobs._launch"
        ) as launched, mock.patch(
            # Repo validation reads real checkouts; this test is
            # about the run flow, so the repo is stubbed.
            "backend.core.get_repo_status",
            return_value={"repo": "remote-server"},
        ):
            resp = self.client.post(
                "/api/jobs/run-td", json={"command": "td context-warm"}
            )
            self.assertEqual(resp.status_code, 200)
            rid = resp.json()["id"]
            launched.assert_called_once_with(
                rid, "run-td", "remote-server",
                td_argv=["td", "context-warm"])
            # Queue (#1025): the engine holds two slots, so the
            # second run starts too instead of conflicting.
            resp2 = self.client.post("/api/jobs/run", json={"action": "logs"})
            self.assertEqual(resp2.status_code, 200)
            body2 = resp2.json()
            self.assertFalse(body2["queued"])
            self.assertEqual(body2["position"], 0)
            rid2 = body2["id"]
        # list + clear jobs
        self.assertEqual(self.client.get("/api/jobs").status_code, 200)
        resp3 = self.client.post("/api/jobs/clear", json={})
        self.assertEqual(resp3.status_code, 400)
        resp3 = self.client.post(
            "/api/jobs/clear",
            json={"intent": "jobs-clear", "confirmed": True})
        self.assertEqual(resp3.json(), {"ok": True, "cleared": 0})
        jobs_router.registry.finish_run(rid, 0)
        jobs_router.registry.finish_run(rid2, 0)

    def test_run_accepts_branch(self):
        # Explicit branch (#708): validated, then the run starts
        # exactly as today.
        import backend.api.routers.jobs as jobs_router

        jobs_router.registry = RunRegistry()
        seen = {}

        def fake_branch(repo, branch, run=None):
            seen["repo"] = repo
            seen["branch"] = branch
            return branch

        with mock.patch(
            "backend.api.routers.jobs._launch"
        ), mock.patch(
            "backend.core.get_repo_status",
            return_value={"repo": "remote-server"},
        ), mock.patch(
            "backend.core.validate_action_branch",
            side_effect=fake_branch,
        ):
            resp = self.client.post(
                "/api/jobs/run",
                json={"action": "logs", "repo": "remote-server",
                      "branch": "main"},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(
                seen, {"repo": "remote-server", "branch": "main"})
        jobs_router.registry = RunRegistry()

    def test_run_rejects_unknown_branch(self):
        import backend.api.routers.jobs as jobs_router

        jobs_router.registry = RunRegistry()
        with mock.patch(
            "backend.api.routers.jobs._launch"
        ), mock.patch(
            "backend.core.get_repo_status",
            return_value={"repo": "remote-server"},
        ), mock.patch(
            "backend.core.validate_action_branch",
            side_effect=ValueError("unknown branch: 'gone'"),
        ):
            resp = self.client.post(
                "/api/jobs/run",
                json={"action": "logs", "repo": "remote-server",
                      "branch": "gone"},
            )
            self.assertEqual(resp.status_code, 400)
            self.assertIn("unknown branch", resp.json()["detail"])
            self.assertEqual(self.client.get("/api/jobs").json(), {"jobs": []})
        jobs_router.registry = RunRegistry()

    def test_run_without_branch_validates_blank(self):
        # Omitted branch reaches validation as "" (which the helper
        # short-circuits without git): behavior is today's.
        import backend.api.routers.jobs as jobs_router

        jobs_router.registry = RunRegistry()
        with mock.patch(
            "backend.api.routers.jobs._launch"
        ), mock.patch(
            "backend.core.get_repo_status",
            return_value={"repo": "remote-server"},
        ), mock.patch(
            "backend.core.validate_action_branch",
            return_value="",
        ) as branch_mock:
            resp = self.client.post(
                "/api/jobs/run",
                json={"action": "logs", "repo": "remote-server"},
            )
            self.assertEqual(resp.status_code, 200)
            branch_mock.assert_called_once_with("remote-server", "")
        jobs_router.registry = RunRegistry()

    def test_run_td_accepts_branch(self):
        import backend.api.routers.jobs as jobs_router

        jobs_router.registry = RunRegistry()
        with mock.patch(
            "backend.api.routers.jobs._launch"
        ) as launched, mock.patch(
            "backend.core.get_repo_status",
            return_value={"repo": "remote-server"},
        ), mock.patch(
            "backend.core.validate_action_branch",
            return_value="main",
        ) as branch_mock:
            resp = self.client.post(
                "/api/jobs/run-td",
                json={"command": "td context-warm",
                      "repo": "remote-server", "branch": "main"},
            )
            self.assertEqual(resp.status_code, 200)
            branch_mock.assert_called_once_with("remote-server", "main")
            launched.assert_called_once()
        jobs_router.registry = RunRegistry()

    def test_clear_all_conflicts_while_active(self):
        # Endpoint pins #512: clear-all refuses with 409 naming a
        # still-visible active run; with the queue (#1025) extra
        # dispatches wait instead of conflicting.
        import backend.api.routers.jobs as jobs_router

        jobs_router.registry = RunRegistry()
        rid, err = jobs_router.registry.start_run("logs")
        self.assertIsNone(err)
        extra_ids = []
        try:
            resp = self.client.post(
                "/api/jobs/clear",
                json={"all": True, "intent": "jobs-clear",
                      "confirmed": True})
            self.assertEqual(resp.status_code, 409)
            listed = self.client.get("/api/jobs").json()["jobs"]
            self.assertEqual([j["id"] for j in listed], [rid])
            # The refusal names the still-visible active run (#512),
            # never an invisible one.
            self.assertIn(rid, resp.json()["detail"])
            # Queue (#1025): a second dispatch takes the free slot
            # and a third waits queued — neither conflicts.
            resp2 = self.client.post(
                "/api/jobs/run", json={"action": "logs"})
            self.assertEqual(resp2.status_code, 200)
            body2 = resp2.json()
            self.assertFalse(body2["queued"])
            self.assertEqual(body2["position"], 0)
            extra_ids.append(body2["id"])
            resp_q = self.client.post(
                "/api/jobs/run", json={"action": "logs"})
            self.assertEqual(resp_q.status_code, 200)
            body_q = resp_q.json()
            self.assertTrue(body_q["queued"])
            self.assertEqual(body_q["position"], 1)
            extra_ids.append(body_q["id"])
        finally:
            jobs_router.registry.finish_run(rid, 0)
            for extra in extra_ids:
                jobs_router.registry.finish_run(extra, 0)
        resp3 = self.client.post(
            "/api/jobs/clear",
            json={"all": True, "intent": "jobs-clear",
                  "confirmed": True})
        self.assertEqual(resp3.json(), {"ok": True, "cleared": 3})

    def test_run_destructive_requires_confirm(self):
        import backend.api.routers.jobs as jobs_router

        jobs_router.registry = RunRegistry()
        resp = self.client.post(
            "/api/jobs/run", json={"action": "recreate-code"})
        self.assertEqual(resp.status_code, 400)
        with mock.patch(
            "backend.api.routers.jobs._launch"
        ) as launched:
            resp = self.client.post(
                "/api/jobs/run",
                json={"action": "recreate-code", "intent": "jobs-run",
                      "confirmed": True})
            self.assertEqual(resp.status_code, 200)
            launched.assert_called_once_with(
                resp.json()["id"], "recreate-code", "remote-server",
                td_argv=None)
        jobs_router.registry.finish_run(resp.json()["id"], 0)

    def test_run_recreate_rejected_no_resume_path(self):
        # Terminal restart drops live agent sessions with no resume
        # path, so the console must not offer it (#444): the engine
        # allowlist, the destructive set, and the step builder all
        # reject it, and the router answers 400 even with confirm.
        import backend.api.routers.jobs as jobs_router

        jobs_router.registry = RunRegistry()
        self.assertNotIn("recreate", engine.ALLOWED_ACTIONS)
        self.assertNotIn("recreate", engine.DESTRUCTIVE_ACTIONS)
        steps, err = engine.action_steps(
            "recreate", "remote-server", "", "")
        self.assertIsNone(steps)
        self.assertEqual(err, "unknown action")
        for body in ({"action": "recreate"},
                     {"action": "recreate", "intent": "jobs-run",
                      "confirmed": True}):
            resp = self.client.post("/api/jobs/run", json=body)
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(resp.json()["detail"], "unknown action")

    def test_run_repo_scoped_validates_repo(self):
        resp = self.client.post(
            "/api/jobs/run",
            json={"action": "preflight", "repo": "no-such-repo"})
        self.assertEqual(resp.status_code, 400)

    def test_jobs_list_carries_timestamps(self):
        import backend.api.routers.jobs as jobs_router

        jobs_router.registry = RunRegistry()
        rid, err = jobs_router.registry.start_run("logs")
        self.assertIsNone(err)
        resp = self.client.get("/api/jobs")
        self.assertEqual(resp.status_code, 200)
        rows = [j for j in resp.json()["jobs"] if j["id"] == rid]
        self.assertEqual(len(rows), 1)
        self.assertIsInstance(rows[0]["at"], float)
        jobs_router.registry.finish_run(rid, 0)

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

    def test_replay_fields_on_progress_and_rows_td(self):
        import backend.api.routers.jobs as jobs_router

        jobs_router.registry = RunRegistry()
        rid, err = jobs_router.registry.start_run(
            "run-td", "remote-server", td_argv=["td", "context-warm"])
        self.assertIsNone(err)
        resp = self.client.get("/api/jobs")
        rows = [j for j in resp.json()["jobs"] if j["id"] == rid]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["td_argv"], ["td", "context-warm"])
        self.assertEqual(rows[0]["repo"], "remote-server")
        resp = self.client.get(f"/api/jobs/runs/{rid}")
        body = resp.json()
        self.assertEqual(body["td_argv"], ["td", "context-warm"])
        self.assertEqual(body["repo"], "remote-server")
        self.assertEqual(body["action"], "run-td")
        jobs_router.registry.finish_run(rid, 0)

    def test_replay_fields_default_empty_for_plain(self):
        import backend.api.routers.jobs as jobs_router

        jobs_router.registry = RunRegistry()
        rid, err = jobs_router.registry.start_run("logs")
        self.assertIsNone(err)
        resp = self.client.get("/api/jobs")
        rows = [j for j in resp.json()["jobs"] if j["id"] == rid]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["td_argv"], [])
        resp = self.client.get(f"/api/jobs/runs/{rid}")
        self.assertEqual(resp.json()["td_argv"], [])
        jobs_router.registry.finish_run(rid, 0)

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


class CompareEndpointTest(unittest.TestCase):
    """GET /api/jobs/runs/compare (#657)."""

    def setUp(self):
        import backend.api.routers.jobs as jobs_router

        from backend.server import app

        jobs_router.registry = RunRegistry()
        self.reg = jobs_router.registry
        self.client = TestClient(app)

    def _make_run(self, output):
        rid, err = self.reg.start_run("logs")
        self.assertIsNone(err)
        self.reg.append(rid, output)
        self.reg.finish_run(rid, 0)
        return rid

    def test_identical_outputs_empty_diff(self):
        out = "line1\nline2\nok\n"
        a = self._make_run(out)
        b = self._make_run(out)
        resp = self.client.get(
            "/api/jobs/runs/compare", params={"a": a, "b": b})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["a_id"], a)
        self.assertEqual(body["b_id"], b)
        self.assertEqual(body["unified_diff"], "")
        self.assertFalse(body["truncated"])

    def test_same_id_empty_diff(self):
        a = self._make_run("x\ny\n")
        resp = self.client.get(
            "/api/jobs/runs/compare", params={"a": a, "b": a})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["unified_diff"], "")

    def test_differing_outputs_hunks(self):
        a = self._make_run("line1\nold value\nline3\n")
        b = self._make_run("line1\nnew value\nline3\n")
        resp = self.client.get(
            "/api/jobs/runs/compare", params={"a": a, "b": b})
        self.assertEqual(resp.status_code, 200)
        diff = resp.json()["unified_diff"]
        self.assertIn(f"--- a/{a}", diff)
        self.assertIn(f"+++ b/{b}", diff)
        self.assertIn("-old value", diff)
        self.assertIn("+new value", diff)
        self.assertIn("@@", diff)

    def test_bad_shape_id_404(self):
        a = self._make_run("x\n")
        resp = self.client.get(
            "/api/jobs/runs/compare", params={"a": a, "b": "nothex"})
        self.assertEqual(resp.status_code, 404)
        resp = self.client.get("/api/jobs/runs/compare",
                               params={"a": a})
        self.assertEqual(resp.status_code, 404)

    def test_missing_and_cleared_id_404(self):
        a = self._make_run("x\n")
        resp = self.client.get(
            "/api/jobs/runs/compare",
            params={"a": a, "b": "aaaaaaaaaaaa"})
        self.assertEqual(resp.status_code, 404)
        with self.reg._lock:
            del self.reg.runs[a]
        b = self._make_run("y\n")
        resp = self.client.get(
            "/api/jobs/runs/compare", params={"a": a, "b": b})
        self.assertEqual(resp.status_code, 404)

    def test_cap_note_on_giant_output(self):
        import backend.api.routers.jobs as jobs_router

        a = self._make_run("small\n")
        b = self._make_run("seed\n")
        big = "L\n" * (jobs_router.COMPARE_MAX_PER_SIDE + 100)
        with self.reg._lock:
            self.reg.runs[b]["output"] = big
        resp = self.client.get(
            "/api/jobs/runs/compare", params={"a": a, "b": b})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["truncated"])
        # Diff computed over the capped tail (250000 L lines), not
        # the full 250050-line text.
        self.assertEqual(body["unified_diff"].count("\n+L"), 250000)


def _parse_frames(text):
    """Split SSE text into (event, data) frames; comments dropped."""
    frames = []
    for chunk in text.split("\n\n"):
        chunk = chunk.strip()
        if not chunk or chunk.startswith(":"):
            continue
        event, data = None, None
        for line in chunk.splitlines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = line.split(":", 1)[1].strip()
        if event is not None:
            import json

            frames.append((event, json.loads(data)))
    return frames


class PushStreamTest(unittest.TestCase):
    def setUp(self):
        import backend.api.routers.jobs as jobs_router

        from backend.server import app

        self.jobs_router = jobs_router
        jobs_router.registry = RunRegistry()
        self.client = TestClient(app)

    def test_version_bumps_on_mutation_only(self):
        reg = self.jobs_router.registry
        self.assertEqual(reg._version, 0)
        rid, err = reg.start_run("logs")
        self.assertIsNone(err)
        self.assertEqual(reg._version, 1)
        reg.append(rid, "hi")
        self.assertEqual(reg._version, 2)
        reg.finish_run(rid, 0)
        self.assertEqual(reg._version, 3)
        # No-ops bump nothing: missing append, empty clear.
        reg.append("deadbeef0000", "x")
        self.assertEqual(reg._version, 3)
        reg2 = RunRegistry()
        _, err_a = reg2.start_run("logs")
        self.assertIsNone(err_a)
        # Queue (#1025): the repeat start waits instead of
        # conflicting, so it bumps like any other mutation.
        _, err_b = reg2.start_run("logs")
        self.assertIsNone(err_b)
        self.assertEqual(reg2._version, 2)
        rid_b, _ = reg.start_run("logs")
        self.assertTrue(rid_b)
        self.assertEqual(reg._version, 4)
        reg.clear_jobs(all_jobs=False)
        self.assertEqual(reg._version, 5)
        reg.clear_jobs(all_jobs=False)
        self.assertEqual(reg._version, 5)

    def test_jobs_stream_pushes_status_not_chatter(self):
        reg = self.jobs_router.registry
        rid, _ = reg.start_run("logs")
        gen = self.jobs_router._jobs_events(heartbeat=60)
        frames = _parse_frames(next(gen))
        self.assertEqual(frames[0][0], "jobs")
        self.assertFalse(frames[0][1]["jobs"][0]["done"])
        # Output appends leave list rows identical: no jobs frame.
        # Drive with a short heartbeat to observe the quiet beat.
        gen.close()
        gen = self.jobs_router._jobs_events(heartbeat=0.02)
        next(gen)
        reg.append(rid, "chatter")
        self.assertTrue(next(gen).startswith(":heartbeat"))
        reg.finish_run(rid, 0)
        frames = _parse_frames(next(gen))
        self.assertEqual(frames[0][0], "jobs")
        self.assertTrue(frames[0][1]["jobs"][0]["done"])
        gen.close()

    def test_jobs_stream_matches_poll_shape(self):
        reg = self.jobs_router.registry
        reg.start_run("logs")
        gen = self.jobs_router._jobs_events(heartbeat=60)
        frames = _parse_frames(next(gen))
        gen.close()
        polled = self.client.get("/api/jobs").json()["jobs"]
        self.assertEqual(frames[0][1]["jobs"], polled)

    def test_run_stream_frames_and_ends_on_done(self):
        reg = self.jobs_router.registry
        rid, _ = reg.start_run("logs")
        gen = self.jobs_router._run_events(rid, heartbeat=60)
        frames = _parse_frames(next(gen))
        self.assertEqual(frames[0][0], "progress")
        self.assertFalse(frames[0][1]["done"])
        reg.append(rid, "out")
        frames = _parse_frames(next(gen))
        self.assertIn("out", frames[0][1]["output"])
        reg.finish_run(rid, 0)
        frames = _parse_frames(next(gen))
        self.assertTrue(frames[0][1]["done"])
        with self.assertRaises(StopIteration):
            next(gen)

    def test_run_stream_cleared_run_errors(self):
        reg = self.jobs_router.registry
        rid, _ = reg.start_run("logs")
        gen = self.jobs_router._run_events(rid, heartbeat=60)
        next(gen)
        # Finished-only clear between frames: the stream ends gone
        # once the record disappears (clear-all refuses active
        # runs, so finish before clearing, #512).
        reg.finish_run(rid, 0)
        reg.clear_jobs(all_jobs=False)
        frames = _parse_frames(next(gen))
        self.assertEqual(frames[0][0], "gone")
        with self.assertRaises(StopIteration):
            next(gen)

    def test_stream_endpoints_wire_and_validate(self):
        import backend.api.routers.jobs as jobs_router

        jobs_router.registry = RunRegistry()
        rid, _ = jobs_router.registry.start_run("logs")
        with self.client.stream("GET", "/api/jobs/stream") as resp:
            self.assertEqual(resp.status_code, 200)
            self.assertIn("text/event-stream", resp.headers["content-type"])
            first = ""
            for chunk in resp.iter_text():
                first += chunk
                if "\n\n" in first:
                    break
        frames = _parse_frames(first)
        self.assertEqual(frames[0][0], "jobs")
        self.assertEqual(frames[0][1]["jobs"][0]["id"], rid)
        self.assertEqual(
            self.client.get("/api/jobs/runs/not-a-rid/stream").status_code,
            404)
        self.assertEqual(
            self.client.get(
                "/api/jobs/runs/deadbeef0000/stream").status_code,
            404)


class _LedgerTmpDir:
    """Point the audit ledger at a tmp dir: execute_run now ledgers
    every finished run, and tests must not touch the real path."""

    def _ledger_setup(self):
        import os
        import tempfile

        self._ledger_tmp = tempfile.TemporaryDirectory()
        self._ledger_old = os.environ.get("BUTTONS_LEDGER_PATH")
        os.environ["BUTTONS_LEDGER_PATH"] = os.path.join(
            self._ledger_tmp.name, "ledger.jsonl")

    def _ledger_teardown(self):
        import os

        if self._ledger_old is None:
            os.environ.pop("BUTTONS_LEDGER_PATH", None)
        else:
            os.environ["BUTTONS_LEDGER_PATH"] = self._ledger_old
        self._ledger_tmp.cleanup()


class ExecuteRunTest(_LedgerTmpDir, unittest.TestCase):
    def setUp(self):
        self._ledger_setup()

    def tearDown(self):
        self._ledger_teardown()

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
        self.assertEqual(reg.active_ids, set())

    def test_failing_step_stops_with_note(self):
        reg, rid = self._reg("status")
        engine.execute_run(
            reg, rid, "status", "remote-server", "", "",
            runner=lambda argv: (1, ""))
        run = reg.runs[rid]
        self.assertTrue(run["done"])
        self.assertEqual(run["exit_code"], 1)
        self.assertIn("printed nothing", run["output"])

    def test_finished_run_is_ledgered(self):
        import json
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            old = os.environ.get("BUTTONS_LEDGER_PATH")
            os.environ["BUTTONS_LEDGER_PATH"] = path
            try:
                reg, rid = self._reg("logs")
                engine.execute_run(
                    reg, rid, "logs", "remote-server", "", "",
                    runner=lambda argv: (0, "ok\n"))
                reg2, rid2 = self._reg("run-td")
                engine.execute_run(
                    reg2, rid2, "run-td", "remote-server",
                    "arii/remote-server", "/srv/remote-server",
                    td_argv=["td", "context-warm"],
                    runner=lambda argv: (0, "ok\n"))
            finally:
                if old is None:
                    os.environ.pop("BUTTONS_LEDGER_PATH", None)
                else:
                    os.environ["BUTTONS_LEDGER_PATH"] = old
            with open(path) as f:
                entries = [json.loads(line) for line in f]
        by_action = {e["action"]: e for e in entries
                     if e.get("kind") == "run"}
        self.assertEqual(by_action["logs"]["exit"], 0)
        self.assertEqual(by_action["logs"]["repo"], "remote-server")
        self.assertIn("elapsed_s", by_action["logs"])
        self.assertEqual(by_action["run-td"]["command"],
                         "td context-warm")

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

    def test_run_plan_builds_validated_step(self):
        argv, err = engine.build_plan_argv("review", "12", [], "")
        self.assertIsNone(err)
        steps, err = engine.action_steps(
            "run-plan", "remote-server", "", "", argv)
        self.assertIsNone(err)
        self.assertEqual(steps, [argv])
        for bad in (None, [], ["td", "context-warm"]):
            steps, err = engine.action_steps(
                "run-plan", "remote-server", "", "", bad)
            self.assertIsNone(steps)
            self.assertIn("validated plan command", err)

    def test_run_plan_run_is_ledgered(self):
        import json
        import os

        argv, err = engine.build_plan_argv("issue-audit", "", [], "")
        self.assertIsNone(err)
        reg, rid = self._reg("run-plan")
        engine.execute_run(
            reg, rid, "run-plan", "remote-server", "", "",
            td_argv=argv,
            runner=lambda step: (0, '{"plan_path": "p.md"}\n'))
        run = reg.runs[rid]
        self.assertTrue(run["done"])
        self.assertEqual(run["exit_code"], 0)
        self.assertIn("plan_path", run["output"])
        path = os.environ["BUTTONS_LEDGER_PATH"]
        with open(path) as f:
            entries = [json.loads(line) for line in f]
        plans = [e for e in entries
                 if e.get("kind") == "run"
                 and e.get("action") == "run-plan"]
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["exit"], 0)
        self.assertEqual(
            plans[0]["command"],
            "td --json agent plan-issue-audit --all-open")


def _sleep_step(seconds):
    return [sys.executable, "-c",
            f"import time;time.sleep({seconds})"]


def _spawner_step(pidfile):
    """A step whose child spawns a grandchild sleeper, records its
    pid in ``pidfile``, then sleeps itself: the watchdog must kill
    both (#445)."""
    prog = (
        "import subprocess,sys,time;"
        "p=subprocess.Popen([sys.executable,'-c',"
        "'import time;time.sleep(30)']);"
        f"open({pidfile!r},'w').write(str(p.pid));"
        "time.sleep(30)"
    )
    return [sys.executable, "-c", prog]


def _pid_alive(pid):
    """True while ``pid`` is a live (non-zombie) process."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers signal 0 but is already dead; its
    # parent is gone so init reaps it — treat it as gone.
    try:
        with open(f"/proc/{pid}/stat") as f:
            state = f.read().rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except OSError:
        return False


def _assert_pid_dead(testcase, pid, grace=5.0):
    """Fail unless ``pid`` dies within ``grace`` seconds; never leak
    the sleeper on failure."""
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.05)
    if not _pid_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    testcase.fail(f"grandchild pid {pid} survived watchdog kill")


class WatchdogTest(_LedgerTmpDir, unittest.TestCase):
    def setUp(self):
        self._ledger_setup()

    def tearDown(self):
        self._ledger_teardown()

    def test_hung_step_killed_by_watchdog(self):
        chunks = []
        start = time.time()
        code = engine.run_step_pipe(_sleep_step(3), chunks.append,
                                    timeout=0.2)
        elapsed = time.time() - start
        self.assertEqual(code, 124)
        self.assertIn("watchdog", "".join(chunks))
        self.assertLess(elapsed, 3)

    def test_hung_step_finishes_run(self):
        import backend.api.routers.jobs as jobs_router

        jobs_router.registry = RunRegistry()
        reg = jobs_router.registry
        rid, err = reg.start_run("logs")
        self.assertIsNone(err)
        with mock.patch.object(engine, "action_steps",
                               return_value=([_sleep_step(3)], None)), \
                mock.patch.object(engine, "STEP_TIMEOUT", 0.2):
            engine.execute_run(reg, rid, "logs", "remote-server", "", "")
        run = reg.runs[rid]
        self.assertTrue(run["done"])
        self.assertEqual(run["exit_code"], 124)
        self.assertIn("watchdog", run["output"])
        self.assertEqual(reg.active_ids, set())

    def test_normal_step_unaffected(self):
        chunks = []
        code = engine.run_step_pipe(
            [sys.executable, "-c", "print('hi')"], chunks.append)
        self.assertEqual(code, 0)
        self.assertIn("hi", "".join(chunks))

    def test_watchdog_kills_grandchildren(self):
        # #445: the step's child spawns a grandchild sleeper; the
        # watchdog must take the whole tree down, not just the
        # child, so a retry never fights an orphan.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            pidfile = os.path.join(tmp, "grandchild.pid")
            chunks = []
            code = engine.run_step_pipe(
                _spawner_step(pidfile), chunks.append, timeout=0.5)
            self.assertEqual(code, 124)
            with open(pidfile) as f:
                grandchild = int(f.read().strip())
            _assert_pid_dead(self, grandchild)


class FinishGuaranteeTest(_LedgerTmpDir, unittest.TestCase):
    def setUp(self):
        self._ledger_setup()

    def tearDown(self):
        self._ledger_teardown()

    def test_append_failure_still_finishes(self):
        reg = RunRegistry()
        rid, err = reg.start_run("logs")
        self.assertIsNone(err)
        with mock.patch.object(reg, "append",
                               side_effect=RuntimeError("store down")):
            engine.execute_run(reg, rid, "logs", "remote-server", "", "",
                               runner=lambda argv: (0, "ok\n"))
        run = reg.runs[rid]
        self.assertTrue(run["done"])
        self.assertEqual(run["exit_code"], 1)
        self.assertEqual(reg.active_ids, set())

    def test_finish_run_clears_missing_record(self):
        reg = RunRegistry()
        reg.active_ids = {"deadbeefcafe"}
        reg.finish_run("deadbeefcafe", 1)
        self.assertEqual(reg.active_ids, set())

    def test_launch_failure_releases_lock(self):
        import backend.api.routers.jobs as jobs_router
        from backend.server import app

        from fastapi.testclient import TestClient

        client = TestClient(app, raise_server_exceptions=False)
        for path, payload in (
            ("/api/jobs/run", {"action": "logs"}),
            ("/api/jobs/run-td", {"command": "td context-warm"}),
            ("/api/jobs/run-plan", {"plan": "issue-audit"}),
        ):
            jobs_router.registry = RunRegistry()
            with mock.patch(
                "backend.api.routers.jobs._launch",
                side_effect=RuntimeError("thread starved"),
            ), mock.patch(
                # Stub the checkout so the test asserts the
                # launch failure, not the absence of /srv/repos.
                "backend.core.get_repo_status",
                return_value={"repo": "remote-server"},
            ):
                resp = client.post(path, json=payload)
            self.assertEqual(resp.status_code, 500)
            self.assertEqual(jobs_router.registry.active_ids, set())
            rid, err = jobs_router.registry.start_run("logs")
            self.assertIsNone(err)
            jobs_router.registry.finish_run(rid, 0)


class AsyncPlanTest(unittest.TestCase):
    def test_run_plan_route_is_sync(self):
        # #452: the handler only starts the run and returns its id —
        # the plan itself runs on a worker, so the route must not be
        # a coroutine awaiting anything inline.
        import backend.api.routers.jobs as jobs_router

        self.assertFalse(
            inspect.iscoroutinefunction(jobs_router.start_run_plan))

    def test_td_run_async_echo(self):
        out = asyncio.run(engine.td_run_async("echo hi"))
        self.assertEqual(out, "hi\n")

    def test_td_run_async_timeout_kills(self):
        start = time.time()
        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(engine.td_run_async("sleep 5", timeout=0.2))
        self.assertIn("timed out", str(ctx.exception))
        self.assertLess(time.time() - start, 5)

    def test_td_run_async_timeout_kills_grandchildren(self):
        # #445: same whole-tree kill for the async path — the
        # bash child's grandchild must not survive the timeout.
        import shlex
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            pidfile = os.path.join(tmp, "grandchild.pid")
            cmd = " ".join(
                shlex.quote(a) for a in _spawner_step(pidfile))
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(engine.td_run_async(cmd, timeout=0.5))
            self.assertIn("timed out", str(ctx.exception))
            with open(pidfile) as f:
                grandchild = int(f.read().strip())
            _assert_pid_dead(self, grandchild)

    def test_td_run_async_overlaps(self):
        async def _two():
            await asyncio.gather(
                engine.td_run_async("sleep 0.5"),
                engine.td_run_async("sleep 0.5"))
        start = time.time()
        asyncio.run(_two())
        self.assertLess(time.time() - start, 1.0)


class CancelRunTest(_LedgerTmpDir, unittest.TestCase):
    """Cancel a running run: tree kill, exit 130, endpoint codes (#589)."""

    def setUp(self):
        self._ledger_setup()
        import backend.api.routers.jobs as jobs_router

        self._jobs_router = jobs_router
        self._old_registry = jobs_router.registry
        jobs_router.registry = RunRegistry()
        from backend.server import app

        self.client = TestClient(app)

    def tearDown(self):
        self._jobs_router.registry = self._old_registry
        self._ledger_teardown()

    @staticmethod
    def _wait_for_proc(reg, rid, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with reg._lock:
                proc = reg.runs[rid].get("_proc")
            if proc is not None:
                return proc
            time.sleep(0.05)
        raise AssertionError("step proc was never registered")

    @staticmethod
    def _wait_for_pidfile(pidfile, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with open(pidfile) as fh:
                    return int(fh.read().strip())
            except (OSError, ValueError):
                time.sleep(0.05)
        raise AssertionError("child pid was never written")

    def _assert_grandchild_dead(self, child, timeout=5):
        # kill(pid, 0) lies about zombies, so read /proc state: the
        # group SIGTERM kills the grandchild, which reparents out of
        # our reach — fully gone or a zombie awaiting init's reap
        # both prove the kill landed.
        import os

        if not os.path.isdir("/proc"):
            self.skipTest("grandchild liveness needs /proc")
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with open(f"/proc/{child}/stat") as fh:
                    state = fh.read().rsplit(")", 1)[1].split()[0]
            except FileNotFoundError:
                return
            if state == "Z":
                return
            time.sleep(0.05)
        raise AssertionError(f"grandchild {child} still alive")

    def test_cancel_unknown_run(self):
        reg = RunRegistry()
        rid, err = reg.cancel_run("deadbeef1234")
        self.assertIsNone(rid)
        self.assertEqual(err, "unknown run")

    def test_cancel_finished_run_is_not_active(self):
        reg = RunRegistry()
        rid, err = reg.start_run("logs")
        self.assertIsNone(err)
        reg.finish_run(rid, 0)
        rid2, err2 = reg.cancel_run(rid)
        self.assertIsNone(rid2)
        self.assertEqual(err2, "run is not active")

    def test_cancel_endpoint_404_unknown(self):
        resp = self.client.post("/api/jobs/runs/deadbeef1234/cancel")
        self.assertEqual(resp.status_code, 404)

    def test_cancel_endpoint_404_bad_rid(self):
        resp = self.client.post("/api/jobs/runs/nope/cancel")
        self.assertEqual(resp.status_code, 404)

    def test_cancel_endpoint_409_finished(self):
        reg = self._jobs_router.registry
        rid, err = reg.start_run("logs")
        self.assertIsNone(err)
        reg.finish_run(rid, 0)
        resp = self.client.post(f"/api/jobs/runs/{rid}/cancel")
        self.assertEqual(resp.status_code, 409)

    def test_cancel_endpoint_202_sets_flag(self):
        reg = self._jobs_router.registry
        rid, err = reg.start_run("logs")
        self.assertIsNone(err)
        try:
            resp = self.client.post(f"/api/jobs/runs/{rid}/cancel")
            self.assertEqual(resp.status_code, 202)
            body = resp.json()
            self.assertTrue(body["ok"])
            self.assertEqual(body["id"], rid)
            self.assertTrue(reg.cancel_requested(rid))
        finally:
            reg.finish_run(rid, 0)

    def test_cancel_kills_process_tree(self):
        import os
        import threading

        reg = RunRegistry()
        pidfile = os.path.join(self._ledger_tmp.name, "child.pid")
        parent = [sys.executable, "-c",
                  "import subprocess,sys,time;"
                  "p=subprocess.Popen([sys.executable,'-c',"
                  "'import time;time.sleep(20)']);"
                  f"open({pidfile!r},'w').write(str(p.pid));"
                  "time.sleep(20)"]
        rid, err = reg.start_run("logs")
        self.assertIsNone(err)
        thread = threading.Thread(
            target=engine.execute_run,
            args=(reg, rid, "logs", "remote-server", "", ""),
            daemon=True)
        with mock.patch.object(engine, "action_steps",
                               return_value=([parent], None)):
            thread.start()
            proc = self._wait_for_proc(reg, rid)
            child = self._wait_for_pidfile(pidfile)
            start = time.time()
            rid2, err2 = reg.cancel_run(rid)
            self.assertIsNone(err2)
            thread.join(timeout=15)
        elapsed = time.time() - start
        self.assertFalse(thread.is_alive())
        self.assertLess(elapsed, 15)
        run = reg.runs[rid]
        self.assertTrue(run["done"])
        self.assertEqual(run["exit_code"], 130)
        self.assertIn("cancelled by operator", run["output"])
        self.assertEqual(reg.active_ids, set())
        self.assertIsNotNone(proc.poll())
        self._assert_grandchild_dead(child)

    def test_cancel_between_steps_skips_remaining(self):
        import threading

        reg = RunRegistry()
        rid, err = reg.start_run("logs")
        self.assertIsNone(err)
        release = threading.Event()
        calls = []

        def _runner(argv):
            calls.append(argv)
            release.wait(timeout=10)
            return 0, "step ok\n"

        thread = threading.Thread(
            target=engine.execute_run,
            args=(reg, rid, "logs", "remote-server", "", ""),
            kwargs={"runner": _runner}, daemon=True)
        steps = [["echo", "one"], ["echo", "two"]]
        with mock.patch.object(engine, "action_steps",
                               return_value=(steps, None)):
            thread.start()
            deadline = time.time() + 10
            while not calls and time.time() < deadline:
                time.sleep(0.02)
            self.assertTrue(calls, "first step never started")
            rid2, err2 = reg.cancel_run(rid)
            self.assertIsNone(err2)
            release.set()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(calls), 1)
        run = reg.runs[rid]
        self.assertTrue(run["done"])
        self.assertEqual(run["exit_code"], 130)
        self.assertIn("cancelled by operator", run["output"])
        self.assertEqual(reg.active_ids, set())


class TriageFanoutTest(_LedgerTmpDir, unittest.TestCase):
    """triage-fanout as a first-class jobs action (#670)."""

    def setUp(self):
        import os
        import tempfile

        import backend.api.routers.jobs as jobs_router

        self._ledger_setup()
        self._jobs_router = jobs_router
        self._old_registry = jobs_router.registry
        jobs_router.registry = RunRegistry()
        from backend.server import app

        self.client = TestClient(app)
        # Fake deep-agent tree: venv python + script exist, so the
        # branch builds argv instead of failing resolution.
        self._agent_tmp = tempfile.TemporaryDirectory()
        venv_bin = os.path.join(self._agent_tmp.name, ".venv", "bin")
        os.makedirs(venv_bin)
        self.fake_python = os.path.join(venv_bin, "python")
        with open(self.fake_python, "w") as f:
            f.write("#!/bin/sh\n")
        self.fake_script = os.path.join(
            self._agent_tmp.name, "triage_fanout.py")
        with open(self.fake_script, "w") as f:
            f.write("# fanout\n")
        self._old_agent_dir = os.environ.get("DEEP_AGENT_DIR")
        self._old_agent_venv = os.environ.get("DEEP_AGENT_VENV")
        os.environ["DEEP_AGENT_DIR"] = self._agent_tmp.name
        os.environ.pop("DEEP_AGENT_VENV", None)

    def tearDown(self):
        import os

        self._jobs_router.registry = self._old_registry
        if self._old_agent_dir is None:
            os.environ.pop("DEEP_AGENT_DIR", None)
        else:
            os.environ["DEEP_AGENT_DIR"] = self._old_agent_dir
        if self._old_agent_venv is None:
            os.environ.pop("DEEP_AGENT_VENV", None)
        else:
            os.environ["DEEP_AGENT_VENV"] = self._old_agent_venv
        self._agent_tmp.cleanup()
        self._ledger_teardown()

    def test_registered_repo_scoped_and_schedulable(self):
        self.assertIn("triage-fanout", engine.ALLOWED_ACTIONS)
        self.assertIn("triage-fanout", engine.REPO_SCOPED_ACTIONS)
        # Non-destructive: the #655 schedule endpoint accepts any
        # non-destructive allowlisted action, so fanout is
        # schedulable the day it registers.
        self.assertNotIn("triage-fanout", engine.DESTRUCTIVE_ACTIONS)

    def test_argv_shape_repo_substitution_no_prefer(self):
        steps, err = engine.action_steps(
            "triage-fanout", "boomtick", "arii/boomtick",
            "/srv/repos/boomtick")
        self.assertIsNone(err)
        self.assertEqual(
            steps, [[self.fake_python, self.fake_script, "arii/boomtick"]])
        # --prefer omitted: the script's auto ladder applies.
        self.assertFalse(
            any(a.startswith("--prefer") for s in steps for a in s))

    def test_missing_venv_fails_with_clear_message(self):
        import os

        os.remove(self.fake_python)
        steps, err = engine.action_steps(
            "triage-fanout", "boomtick", "arii/boomtick",
            "/srv/repos/boomtick")
        self.assertIsNone(steps)
        self.assertIn("DEEP_AGENT_VENV", err)
        self.assertNotIn("/home/", err)

    def test_missing_script_fails_with_clear_message(self):
        import os

        os.remove(self.fake_script)
        steps, err = engine.action_steps(
            "triage-fanout", "boomtick", "arii/boomtick",
            "/srv/repos/boomtick")
        self.assertIsNone(steps)
        self.assertIn("triage_fanout.py", err)

    def test_timeout_floor_and_inheritance(self):
        # Local runs take 10+ min: the floor is 15 min (900s).
        self.assertGreaterEqual(
            engine.step_timeout("triage-fanout"), 900)
        self.assertEqual(engine.step_timeout("triage-fanout"), 1200)
        self.assertEqual(engine.step_timeout("logs"), engine.STEP_TIMEOUT)
        self.assertEqual(engine.step_timeout("run-td"), engine.STEP_TIMEOUT)

    def test_execute_run_threads_action_budget(self):
        reg = RunRegistry()
        rid, err = reg.start_run("triage-fanout", "boomtick")
        self.assertIsNone(err)
        seen = {}
        real_pipe = engine.run_step_pipe

        def _spy(step, append, runner=None, timeout=None, on_proc=None,
                 env=None, cwd=None):
            seen["timeout"] = timeout
            return real_pipe(step, append, runner=runner,
                             timeout=timeout, on_proc=on_proc, env=env,
                             cwd=cwd)

        with mock.patch.object(engine, "run_step_pipe",
                               side_effect=_spy):
            engine.execute_run(
                reg, rid, "triage-fanout", "boomtick", "arii/boomtick",
                "/srv/repos/boomtick",
                runner=lambda argv: (0, "fanout: done\n"))
        self.assertEqual(seen["timeout"], 1200)
        self.assertTrue(reg.runs[rid]["done"])
        self.assertEqual(reg.runs[rid]["exit_code"], 0)

    def test_launch_queue_rerun_cancel(self):
        import backend.api.routers.jobs as jobs_router

        with mock.patch.object(jobs_router, "_launch"), mock.patch(
            "backend.core.get_repo_status",
            return_value={"repo": "boomtick"},
        ):
            resp = self.client.post(
                "/api/jobs/run",
                json={"action": "triage-fanout", "repo": "boomtick"})
            self.assertEqual(resp.status_code, 200)
            rid = resp.json()["id"]
            self.assertFalse(resp.json()["queued"])
            # Queue (#1025): repeat launches take the second slot,
            # then wait queued — no conflict while active.
            resp2 = self.client.post(
                "/api/jobs/run",
                json={"action": "triage-fanout", "repo": "boomtick"})
            self.assertEqual(resp2.status_code, 200)
            self.assertFalse(resp2.json()["queued"])
            rid2 = resp2.json()["id"]
            resp3 = self.client.post(
                "/api/jobs/run",
                json={"action": "triage-fanout", "repo": "boomtick"})
            self.assertEqual(resp3.status_code, 200)
            body3 = resp3.json()
            self.assertTrue(body3["queued"])
            self.assertEqual(body3["position"], 1)
            # History rows carry the action + repo.
            ids = {rid, rid2, body3["id"]}
            rows = [j for j in self.client.get("/api/jobs").json()["jobs"]
                    if j["id"] in ids]
            self.assertEqual(len(rows), 3)
            for row in rows:
                self.assertEqual(row["kind"], "triage-fanout")
                self.assertEqual(row["repo"], "boomtick")
            # Cancel works on the active fanout run.
            resp_c = self.client.post(f"/api/jobs/runs/{rid}/cancel")
            self.assertEqual(resp_c.status_code, 202)
            jobs_router.registry.finish_run(rid, 130)
            # Re-run replays action+repo like any plain action.
            resp4 = self.client.post(
                "/api/jobs/run",
                json={"action": "triage-fanout", "repo": "boomtick"})
            self.assertEqual(resp4.status_code, 200)
            jobs_router.registry.finish_run(rid2, 0)
            jobs_router.registry.finish_run(body3["id"], 0)
            jobs_router.registry.finish_run(resp4.json()["id"], 0)

    def test_unknown_repo_rejected(self):
        resp = self.client.post(
            "/api/jobs/run",
            json={"action": "triage-fanout", "repo": "no-such-repo"})
        self.assertEqual(resp.status_code, 400)

    def test_schedulable_through_action_schedules(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sched.json")
            old = os.environ.get("BUTTONS_JOBS_SCHEDULE_PATH")
            os.environ["BUTTONS_JOBS_SCHEDULE_PATH"] = path
            try:
                with mock.patch(
                    "backend.api.routers.issues._ensure_schedule_ticker"
                ), mock.patch(
                    "backend.core.get_repo_status",
                    return_value={"repo": "boomtick"},
                ):
                    resp = self.client.post(
                        "/api/jobs/schedule",
                        json={"action": "triage-fanout",
                              "repo": "boomtick", "cadence": "daily"})
                    self.assertEqual(resp.status_code, 200)
                    sched = resp.json()["schedule"]
                    self.assertEqual(sched["action"], "triage-fanout")
                    self.assertEqual(sched["repo"], "boomtick")
                    listed = self.client.get(
                        "/api/jobs/schedule").json()["schedules"]
                    self.assertEqual(len(listed), 1)
            finally:
                if old is None:
                    os.environ.pop("BUTTONS_JOBS_SCHEDULE_PATH", None)
                else:
                    os.environ["BUTTONS_JOBS_SCHEDULE_PATH"] = old

    def test_gated_merge_schedulable_merge_still_locked(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sched.json")
            old = os.environ.get("BUTTONS_JOBS_SCHEDULE_PATH")
            os.environ["BUTTONS_JOBS_SCHEDULE_PATH"] = path
            try:
                with mock.patch(
                    "backend.api.routers.issues._ensure_schedule_ticker"
                ), mock.patch(
                    "backend.core.get_repo_status",
                    return_value={"repo": "boomtick"},
                ):
                    resp = self.client.post(
                        "/api/jobs/schedule",
                        json={"action": "merge-approved-gated",
                              "repo": "boomtick", "cadence": "daily"})
                    self.assertEqual(resp.status_code, 200)
                    self.assertEqual(
                        resp.json()["schedule"]["action"],
                        "merge-approved-gated")
                    # Manual merge-approved keeps its confirm lock:
                    # still unschedulable.
                    resp = self.client.post(
                        "/api/jobs/schedule",
                        json={"action": "merge-approved",
                              "repo": "boomtick", "cadence": "daily"})
                    self.assertEqual(resp.status_code, 400)
            finally:
                if old is None:
                    os.environ.pop("BUTTONS_JOBS_SCHEDULE_PATH", None)
                else:
                    os.environ["BUTTONS_JOBS_SCHEDULE_PATH"] = old


class RunStorePersistenceTest(unittest.TestCase):
    """Restart recovery: the run store rehydrates history (#461)."""

    def setUp(self):
        import os
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "runs.json")

    def tearDown(self):
        self._tmp.cleanup()

    def _read_store(self):
        import json

        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_restart_preserves_completed_runs(self):
        reg = RunRegistry(store_path=self.path)
        first, err = reg.start_run("logs")
        self.assertIsNone(err)
        reg.append(first, "$ echo hi\nhi\n")
        reg.finish_run(first, 0)
        second, err = reg.start_run(
            "run-td", "boomtick", td_argv=["td", "doctor"])
        self.assertIsNone(err)
        reg.append(second, "diag output\n")
        reg.finish_run(second, 1)
        # "Restart": a fresh registry over the same store.
        boot = RunRegistry(store_path=self.path)
        self.assertEqual(boot.active_ids, set())
        self.assertEqual(set(boot.runs), {first, second})
        for rid, exit_code, tail in ((first, 0, "hi\n"),
                                     (second, 1, "diag output\n")):
            run = boot.runs[rid]
            self.assertTrue(run["done"])
            self.assertEqual(run["exit_code"], exit_code)
            self.assertIn(tail, run["output"])
            self.assertNotIn("restarted", run["output"])
        self.assertEqual(boot.runs[second]["td_argv"], ["td", "doctor"])
        self.assertEqual(boot.runs[second]["repo"], "boomtick")
        # Single-flight is free and seqs keep increasing (no
        # collision with rehydrated rows).
        top = max(r["seq"] for r in boot.runs.values())
        third, err = boot.start_run("logs")
        self.assertIsNone(err)
        self.assertGreater(boot.runs[third]["seq"], top)

    def test_restart_marks_interrupted_run_done_with_note(self):
        reg = RunRegistry(store_path=self.path)
        rid, err = reg.start_run("deploy-buttons")
        self.assertIsNone(err)
        # Past the append throttle, like a long-lived run: the
        # partial tail reaches the store before the process dies.
        reg._last_save = 0.0
        reg.append(rid, "deploying...\n")
        # No finish_run: the console restarts mid-run.
        boot = RunRegistry(store_path=self.path)
        run = boot.runs[rid]
        self.assertTrue(run["done"])
        self.assertIsNone(run["exit_code"])
        self.assertTrue(run["output"].startswith("deploying...\n"))
        self.assertTrue(run["output"].endswith(engine.RESTART_NOTE))
        self.assertIsNotNone(run["finished_at"])
        # The interrupted run never wedges single-flight.
        nxt, err = boot.start_run("logs")
        self.assertIsNone(err)

    def test_append_persist_is_throttled(self):
        reg = RunRegistry(store_path=self.path)
        rid, err = reg.start_run("logs")
        self.assertIsNone(err)
        reg._last_save = 0.0
        reg.append(rid, "first\n")
        self.assertIn("first",
                      self._read_store()["runs"][0]["output"])
        reg.append(rid, "second\n")
        self.assertNotIn("second",
                         self._read_store()["runs"][0]["output"])
        reg.finish_run(rid, 0)
        self.assertIn("second",
                      self._read_store()["runs"][0]["output"])

    def test_clear_persists_across_restart(self):
        reg = RunRegistry(store_path=self.path)
        rid, err = reg.start_run("logs")
        self.assertIsNone(err)
        reg.finish_run(rid, 0)
        self.assertEqual(reg.clear_jobs(), (1, None))
        boot = RunRegistry(store_path=self.path)
        self.assertEqual(boot.runs, {})

    def test_missing_or_corrupt_store_loads_empty(self):
        boot = RunRegistry(store_path=self.path)
        self.assertEqual(boot.runs, {})
        self.assertEqual(boot._seq, 0)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        boot = RunRegistry(store_path=self.path)
        self.assertEqual(boot.runs, {})
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('{"runs": [{"no": "id"}, 42, {"id": "abc123"}]}')
        boot = RunRegistry(store_path=self.path)
        self.assertEqual(set(boot.runs), {"abc123"})
        self.assertTrue(boot.runs["abc123"]["done"])

    def test_default_registry_stays_in_memory(self):
        reg = RunRegistry()
        self.assertIsNone(reg._store_path)
        rid, err = reg.start_run("logs")
        self.assertIsNone(err)
        reg.append(rid, "x\n")
        reg.finish_run(rid, 0)
        self.assertIn(rid, reg.runs)

    def test_runs_store_path_env_override(self):
        import os

        old = os.environ.get("BUTTONS_JOBS_RUNS_PATH")
        os.environ["BUTTONS_JOBS_RUNS_PATH"] = self.path
        try:
            self.assertEqual(engine.runs_store_path(), self.path)
        finally:
            if old is None:
                os.environ.pop("BUTTONS_JOBS_RUNS_PATH", None)
            else:
                os.environ["BUTTONS_JOBS_RUNS_PATH"] = old
        self.assertEqual(engine.runs_store_path(),
                         engine.DEFAULT_RUNS_PATH)

    def test_jobs_view_survives_restart(self):
        import backend.api.routers.jobs as jobs_router
        from backend.server import app

        client = TestClient(app)
        jobs_router.registry = RunRegistry(store_path=self.path)
        try:
            rid, err = jobs_router.registry.start_run("logs")
            self.assertIsNone(err)
            jobs_router.registry.append(rid, "tail line\n")
            jobs_router.registry.finish_run(rid, 0)
            before = client.get("/api/jobs").json()["jobs"]
            self.assertEqual(len(before), 1)
            # "Restart": swap in a fresh registry over the same store.
            jobs_router.registry = RunRegistry(store_path=self.path)
            after = client.get("/api/jobs").json()["jobs"]
            self.assertEqual(after, before)
            progress = client.get(f"/api/jobs/runs/{rid}").json()
            self.assertTrue(progress["done"])
            self.assertIn("tail line", progress["output"])
        finally:
            jobs_router.registry = RunRegistry()


class JobsClearFailedAcceptanceTest(unittest.TestCase):
    """#451(1): clear after a failed run clears it, honestly."""

    def setUp(self):
        from backend.server import app

        self.client = TestClient(app)

    def test_clear_failed_run_returns_cleared_and_empties_list(self):
        import backend.api.routers.jobs as jobs_router

        jobs_router.registry = RunRegistry()
        try:
            rid, err = jobs_router.registry.start_run("logs")
            self.assertIsNone(err)
            jobs_router.registry.append(rid, "boom\n")
            jobs_router.registry.finish_run(rid, 1)
            resp = self.client.post(
                "/api/jobs/clear",
                json={"intent": "jobs-clear", "confirmed": True})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json(), {"ok": True, "cleared": 1})
            self.assertEqual(
                self.client.get("/api/jobs").json(), {"jobs": []})
        finally:
            jobs_router.registry = RunRegistry()


class ListJobsRunsTest(unittest.TestCase):
    """#451: list_jobs reads runs — one collection, newest first."""

    def test_empty_registry_lists_nothing(self):
        self.assertEqual(RunRegistry().list_jobs(), [])

    def test_rows_are_newest_first_with_job_shape(self):
        reg = RunRegistry()
        first, err = reg.start_run("logs")
        self.assertIsNone(err)
        reg.finish_run(first, 0)
        second, err = reg.start_run("run-td", "boomtick",
                                    td_argv=["td", "doctor"])
        self.assertIsNone(err)
        rows = reg.list_jobs()
        self.assertEqual([r["id"] for r in rows], [second, first])
        row = rows[1]
        self.assertEqual(row["name"], "logs")
        self.assertEqual(row["repo"], "remote-server")
        self.assertTrue(row["done"])
        self.assertEqual(row["exit_code"], 0)
        self.assertEqual(row["created_at"],
                         reg.runs[first]["started_at"])
        self.assertEqual(row["error"], "")
        self.assertEqual(rows[0]["td_argv"], ["td", "doctor"])

    def test_failed_run_carries_last_output_line_as_error(self):
        reg = RunRegistry()
        rid, err = reg.start_run("logs")
        self.assertIsNone(err)
        reg.append(rid, "step one ok\nstill going\nbang: no disk\n")
        reg.finish_run(rid, 1)
        (row,) = reg.list_jobs()
        self.assertEqual(row["id"], rid)
        self.assertEqual(row["error"], "bang: no disk")
        self.assertEqual(row["exit_code"], 1)

    def test_failed_run_without_output_names_exit(self):
        reg = RunRegistry()
        rid, err = reg.start_run("logs")
        self.assertIsNone(err)
        reg.finish_run(rid, 130)
        (row,) = reg.list_jobs()
        self.assertEqual(row["error"], "exit 130")

    def test_error_line_is_capped(self):
        reg = RunRegistry()
        rid, err = reg.start_run("logs")
        self.assertIsNone(err)
        reg.append(rid, "x" * (engine.JOB_ERROR_MAX + 50) + "\n")
        reg.finish_run(rid, 1)
        (row,) = reg.list_jobs()
        self.assertEqual(len(row["error"]), engine.JOB_ERROR_MAX)

    def test_running_run_has_no_error(self):
        reg = RunRegistry()
        rid, err = reg.start_run("logs")
        self.assertIsNone(err)
        reg.append(rid, "chatter\n")
        (row,) = reg.list_jobs()
        self.assertFalse(row["done"])
        self.assertIsNone(row["exit_code"])
        self.assertEqual(row["error"], "")

    def test_rows_are_copies(self):
        reg = RunRegistry()
        rid, err = reg.start_run("logs")
        self.assertIsNone(err)
        reg.finish_run(rid, 0)
        (row,) = reg.list_jobs()
        row["done"] = False
        self.assertTrue(reg.runs[rid]["done"])


class CompletedRunEvictionTest(unittest.TestCase):
    """#451(3): completed runs older than COMPLETED_RUN_TTL go."""

    def _finish_aged(self, reg, age_s, exit_code=0):
        rid, err = reg.start_run("logs")
        self.assertIsNone(err)
        reg.finish_run(rid, exit_code)
        with reg._lock:
            reg.runs[rid]["finished_at"] = time.time() - age_s
        return rid

    def test_stale_completed_run_evicted(self):
        reg = RunRegistry()
        rid = self._finish_aged(reg, engine.COMPLETED_RUN_TTL + 10)
        self.assertEqual(reg.evict_completed(), 1)
        self.assertNotIn(rid, reg.runs)

    def test_fresh_completed_run_survives(self):
        reg = RunRegistry()
        rid = self._finish_aged(reg, engine.COMPLETED_RUN_TTL - 60)
        self.assertEqual(reg.evict_completed(), 0)
        self.assertIn(rid, reg.runs)

    def test_active_run_never_evicted(self):
        reg = RunRegistry()
        rid, err = reg.start_run("logs")
        self.assertIsNone(err)
        with reg._lock:
            reg.runs[rid]["started_at"] = \
                time.time() - 10 * engine.COMPLETED_RUN_TTL
        self.assertEqual(reg.evict_completed(), 0)
        self.assertIn(rid, reg.runs)

    def test_done_run_without_timestamps_survives(self):
        reg = RunRegistry()
        rid = self._finish_aged(reg, 0)
        with reg._lock:
            reg.runs[rid]["finished_at"] = None
            reg.runs[rid]["started_at"] = None
        self.assertEqual(reg.evict_completed(), 0)
        self.assertIn(rid, reg.runs)

    def test_done_run_falls_back_to_started_at(self):
        reg = RunRegistry()
        rid = self._finish_aged(reg, 0)
        with reg._lock:
            reg.runs[rid]["finished_at"] = None
            reg.runs[rid]["started_at"] = \
                time.time() - engine.COMPLETED_RUN_TTL - 10
        self.assertEqual(reg.evict_completed(), 1)
        self.assertNotIn(rid, reg.runs)

    def test_evict_noop_bumps_nothing(self):
        reg = RunRegistry()
        rid = self._finish_aged(reg, 0)
        before = reg._version
        self.assertEqual(reg.evict_completed(), 0)
        self.assertEqual(reg._version, before)
        self.assertIn(rid, reg.runs)

    def test_start_sweeps_stale(self):
        reg = RunRegistry()
        old = self._finish_aged(reg, engine.COMPLETED_RUN_TTL + 10)
        fresh, err = reg.start_run("logs")
        self.assertIsNone(err)
        self.assertNotIn(old, reg.runs)
        self.assertIn(fresh, reg.runs)

    def test_finish_sweeps_stale(self):
        reg = RunRegistry()
        old, err = reg.start_run("logs")
        self.assertIsNone(err)
        reg.finish_run(old, 0)
        rid, err = reg.start_run("logs")
        self.assertIsNone(err)
        # Age past TTL after the start sweep ran: only the finish
        # sweep can catch it.
        with reg._lock:
            reg.runs[old]["finished_at"] = \
                time.time() - engine.COMPLETED_RUN_TTL - 10
        reg.finish_run(rid, 0)
        self.assertNotIn(old, reg.runs)
        self.assertIn(rid, reg.runs)

    def test_evict_bumps_and_persists(self):
        import json
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "runs.json")
            reg = RunRegistry(store_path=path)
            rid = self._finish_aged(reg, engine.COMPLETED_RUN_TTL + 10)
            before = reg._version
            self.assertEqual(reg.evict_completed(), 1)
            self.assertEqual(reg._version, before + 1)
            with open(path, encoding="utf-8") as f:
                ids = [r["id"] for r in json.load(f)["runs"]]
            self.assertNotIn(rid, ids)

    def test_boot_rehydrate_drops_stale(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "runs.json")
            reg = RunRegistry(store_path=path)
            rid = self._finish_aged(reg, engine.COMPLETED_RUN_TTL + 10)
            with reg._lock:
                reg._persist_locked()
            boot = RunRegistry(store_path=path)
            self.assertNotIn(rid, boot.runs)
            self.assertEqual(boot.runs, {})


class CheckNotificationsActionTest(unittest.TestCase):
    """check-notifications action registration + step shape (#822)."""

    def test_registered_repo_scoped_and_schedulable(self):
        self.assertIn("check-notifications", engine.ALLOWED_ACTIONS)
        self.assertIn("check-notifications", engine.REPO_SCOPED_ACTIONS)
        self.assertNotIn("check-notifications",
                         engine.DESTRUCTIVE_ACTIONS)

    def test_step_invokes_notify_check_module(self):
        import sys

        steps, err = engine.action_steps(
            "check-notifications", "boomtick", "arii/boomtick",
            "/srv/repos/boomtick")
        self.assertIsNone(err)
        self.assertEqual(
            steps, [[sys.executable, "-m", "backend.notify_check",
                     "--repo", "boomtick", "--gh", "arii/boomtick"]])

    def test_step_needs_a_repo(self):
        steps, err = engine.action_steps(
            "check-notifications", "boomtick", "", "")
        self.assertIsNone(steps)
        self.assertIn("needs a repo", err)

    def test_start_run_accepts_action(self):
        reg = RunRegistry()
        rid, err = reg.start_run("check-notifications", "boomtick")
        self.assertIsNone(err)
        self.assertIsNotNone(rid)
        reg.finish_run(rid, 0)


class GatedMergeActionTest(unittest.TestCase):
    """merge-approved-gated action registration + step shape (#867)."""

    def test_registered_repo_scoped_and_schedulable(self):
        self.assertIn("merge-approved-gated", engine.ALLOWED_ACTIONS)
        self.assertIn("merge-approved-gated",
                      engine.REPO_SCOPED_ACTIONS)
        self.assertNotIn("merge-approved-gated",
                         engine.DESTRUCTIVE_ACTIONS)

    def test_step_invokes_merge_gated_module(self):
        import sys

        steps, err = engine.action_steps(
            "merge-approved-gated", "boomtick", "arii/boomtick",
            "/srv/repos/boomtick")
        self.assertIsNone(err)
        self.assertEqual(
            steps, [[sys.executable, "-m", "backend.merge_gated",
                     "--repo", "boomtick", "--gh", "arii/boomtick"]])

    def test_step_needs_a_repo(self):
        steps, err = engine.action_steps(
            "merge-approved-gated", "boomtick", "", "")
        self.assertIsNone(steps)
        self.assertIn("needs a repo", err)

    def test_start_run_accepts_action(self):
        reg = RunRegistry()
        rid, err = reg.start_run("merge-approved-gated", "boomtick")
        self.assertIsNone(err)
        self.assertIsNotNone(rid)
        reg.finish_run(rid, 0)


class StepEnvTest(unittest.TestCase):
    def test_python_m_steps_get_engine_root_pythonpath(self):
        # Steps run with cwd=stack_dir() (stale host bind-mount), so
        # `python -m backend.*` must resolve from this backend's own
        # tree or fail with No module named (live 2026-11-04).
        steps, err = engine.action_steps(
            "check-notifications", "boomtick", "arii/boomtick", "")
        self.assertIsNone(err)
        env = engine.step_env(steps)
        self.assertEqual(env, {"PYTHONPATH": engine.ENGINE_ROOT})

    def test_non_python_dash_m_is_untouched(self):
        # `free -m` (megabytes) must not match the python -m rule.
        self.assertIsNone(engine.step_env([["free", "-m"]]))
        self.assertIsNone(engine.step_env([["td", "run", "x"]]))

    def test_run_step_pipe_merges_env_into_child(self):
        out = []
        code = engine.run_step_pipe(
            [sys.executable, "-c",
             "import os; print(os.environ.get('LANE_PROBE'))"],
            out.append, env={"LANE_PROBE": "yes"})
        self.assertEqual(code, 0)
        self.assertIn("yes", "".join(out))

    def test_python_m_steps_run_from_engine_root(self):
        # cwd shadows PYTHONPATH for the top-level package: a stale
        # stack_dir()/backend (stale tree) wins over /app, so
        # -m steps must execute FROM the deployed tree, not just see it.
        steps, err = engine.action_steps(
            "check-notifications", "boomtick", "arii/boomtick", "")
        self.assertIsNone(err)
        self.assertEqual(engine.step_cwd(steps), engine.ENGINE_ROOT)
        self.assertIsNone(engine.step_cwd([["free", "-m"]]))
        self.assertIsNone(engine.step_cwd([["td", "run", "x"]]))

    def test_run_step_pipe_honors_cwd_override(self):
        out = []
        code = engine.run_step_pipe(
            [sys.executable, "-c", "import os; print(os.getcwd())"],
            out.append, cwd="/tmp")
        self.assertEqual(code, 0)
        self.assertIn("/tmp", "".join(out).strip().splitlines()[-1])


class UxAuditActionTest(_LedgerTmpDir, unittest.TestCase):
    """Dispatched UX audit as a first-class jobs action (#850)."""

    def setUp(self):
        import shutil
        import tempfile

        import backend.api.routers.jobs as jobs_router

        self._ledger_setup()
        self._jobs_router = jobs_router
        self._old_registry = jobs_router.registry
        jobs_router.registry = RunRegistry()
        from backend.server import app

        self.client = TestClient(app)
        # Fake deep-agent tree carrying the REAL runner modules, so
        # the registry/groups paths exercise production code while
        # venv resolution stays hermetic.
        self._agent_tmp = tempfile.TemporaryDirectory()
        real = os.path.join(engine.ENGINE_ROOT, "deep-agent")
        for name in ("ux_audit.py", "cli.py", "impact.py",
                     "s5tools.py"):
            shutil.copy(os.path.join(real, name),
                        self._agent_tmp.name)
        venv_bin = os.path.join(self._agent_tmp.name, ".venv", "bin")
        os.makedirs(venv_bin)
        self.fake_python = os.path.join(venv_bin, "python")
        with open(self.fake_python, "w") as f:
            f.write("#!/bin/sh\n")
        self.fake_script = os.path.join(
            self._agent_tmp.name, "ux_audit.py")
        self._old_agent_dir = os.environ.get("DEEP_AGENT_DIR")
        self._old_agent_venv = os.environ.get("DEEP_AGENT_VENV")
        os.environ["DEEP_AGENT_DIR"] = self._agent_tmp.name
        os.environ.pop("DEEP_AGENT_VENV", None)
        # Hermetic report state for the groups endpoint.
        self._state_tmp = tempfile.TemporaryDirectory()
        self._old_state = os.environ.get("BUTTONS_UX_AUDIT_DIR")
        os.environ["BUTTONS_UX_AUDIT_DIR"] = self._state_tmp.name
        sys.modules.pop("ux_audit", None)

    def tearDown(self):
        self._jobs_router.registry = self._old_registry
        if self._old_agent_dir is None:
            os.environ.pop("DEEP_AGENT_DIR", None)
        else:
            os.environ["DEEP_AGENT_DIR"] = self._old_agent_dir
        if self._old_agent_venv is None:
            os.environ.pop("DEEP_AGENT_VENV", None)
        else:
            os.environ["DEEP_AGENT_VENV"] = self._old_agent_venv
        if self._old_state is None:
            os.environ.pop("BUTTONS_UX_AUDIT_DIR", None)
        else:
            os.environ["BUTTONS_UX_AUDIT_DIR"] = self._old_state
        sys.modules.pop("ux_audit", None)
        self._agent_tmp.cleanup()
        self._state_tmp.cleanup()
        self._ledger_teardown()

    def test_registered_not_destructive_not_repo_scoped(self):
        self.assertIn("ux-audit", engine.ALLOWED_ACTIONS)
        self.assertNotIn("ux-audit", engine.DESTRUCTIVE_ACTIONS)
        # Site-scoped, not repo-scoped: the site rides argv.
        self.assertNotIn("ux-audit", engine.REPO_SCOPED_ACTIONS)
        self.assertEqual(engine.step_timeout("ux-audit"), 1200)

    def test_argv_validation(self):
        self.assertTrue(engine.validate_ux_audit_argv(
            ["ux-audit", "boomtick.blog"]))
        self.assertTrue(engine.validate_ux_audit_argv(
            ["ux-audit", "arii.github.io", "--max-pages", "10"]))
        self.assertFalse(engine.validate_ux_audit_argv(
            ["ux-audit", "boomtick.blog", "--max-pages", "0"]))
        self.assertFalse(engine.validate_ux_audit_argv(
            ["ux-audit", "boomtick.blog", "--max-pages", "201"]))
        self.assertFalse(engine.validate_ux_audit_argv(
            ["ux-audit", "boomtick.blog; rm -rf /"]))
        self.assertFalse(engine.validate_ux_audit_argv(
            ["ux-audit"]))
        self.assertFalse(engine.validate_ux_audit_argv(
            ["run-td", "boomtick.blog"]))

    def test_steps_build_file_run(self):
        steps, err = engine.action_steps(
            "ux-audit", "remote-server", "", "",
            ["ux-audit", "boomtick.blog", "--max-pages", "10"])
        self.assertIsNone(err)
        self.assertEqual(
            steps, [[self.fake_python, self.fake_script, "audit",
                     "boomtick.blog", "--max-pages", "10",
                     "--file"]])

    def test_steps_reject_unvalidated_argv(self):
        steps, err = engine.action_steps(
            "ux-audit", "remote-server", "", "", None)
        self.assertIsNone(steps)
        self.assertIn("ux-audit", err)
        steps, err = engine.action_steps(
            "ux-audit", "remote-server", "", "",
            ["ux-audit", "x;evil"])
        self.assertIsNone(steps)

    def test_dispatch_unknown_site_400(self):
        resp = self.client.post(
            "/api/jobs/run-ux-audit",
            json={"site": "evil.example", "max_pages": 5})
        self.assertEqual(resp.status_code, 400)

    def test_dispatch_bad_cap_400(self):
        resp = self.client.post(
            "/api/jobs/run-ux-audit",
            json={"site": "boomtick.blog", "max_pages": 999})
        self.assertEqual(resp.status_code, 400)

    def test_dispatch_starts_first_class_run(self):
        import backend.api.routers.jobs as jobs_router

        with mock.patch.object(jobs_router, "_launch"):
            resp = self.client.post(
                "/api/jobs/run-ux-audit",
                json={"site": "boomtick.blog", "max_pages": 5})
            self.assertEqual(resp.status_code, 200)
            rid = resp.json()["id"]
            # Queue (#1025): a second dispatch takes the free slot
            # and a third waits queued — neither conflicts.
            resp2 = self.client.post(
                "/api/jobs/run-ux-audit",
                json={"site": "arii.github.io", "max_pages": 5})
            self.assertEqual(resp2.status_code, 200)
            self.assertFalse(resp2.json()["queued"])
            rid2 = resp2.json()["id"]
            resp3 = self.client.post(
                "/api/jobs/run-ux-audit",
                json={"site": "boomtick.blog", "max_pages": 2})
            self.assertEqual(resp3.status_code, 200)
            body3 = resp3.json()
            self.assertTrue(body3["queued"])
            self.assertEqual(body3["position"], 1)
            # The run lists with its replay argv attached.
            listed = self.client.get("/api/jobs").json()["jobs"]
            row = next(j for j in listed if j["id"] == rid)
            self.assertEqual(row["kind"], "ux-audit")
            self.assertEqual(
                row["td_argv"],
                ["ux-audit", "boomtick.blog", "--max-pages", "5"])
            for done_id in (rid, rid2, body3["id"]):
                jobs_router.registry.finish_run(done_id, 0)

    def test_groups_404_until_first_report(self):
        resp = self.client.get(
            "/api/jobs/ux-audit/groups?site=boomtick.blog")
        self.assertEqual(resp.status_code, 404)
        resp = self.client.get(
            "/api/jobs/ux-audit/groups?site=evil.example")
        self.assertEqual(resp.status_code, 400)

    def test_schedule_accepts_valid_audit_argv(self):
        import backend.api.routers.jobs as jobs_router
        import backend.api.routers.issues as issues_router

        with mock.patch.object(
                jobs_router, "load_action_schedules",
                return_value={}), \
                mock.patch.object(jobs_router,
                                  "save_action_schedules"), \
                mock.patch.object(issues_router,
                                  "_ensure_schedule_ticker"):
            resp = self.client.post(
                "/api/jobs/schedule",
                json={"action": "ux-audit", "repo": "remote-server",
                      "cadence": "weekly",
                      "td_argv": ["ux-audit", "boomtick.blog",
                                  "--max-pages", "5"]})
            self.assertEqual(resp.status_code, 200)
            bad = self.client.post(
                "/api/jobs/schedule",
                json={"action": "ux-audit", "repo": "remote-server",
                      "cadence": "weekly",
                      "td_argv": ["ux-audit", "x;evil"]})
            self.assertEqual(bad.status_code, 400)
            missing = self.client.post(
                "/api/jobs/schedule",
                json={"action": "ux-audit", "repo": "remote-server",
                      "cadence": "weekly"})
            self.assertEqual(missing.status_code, 400)

    def test_groups_serve_report_payloads(self):
        import json as json_lib

        report = {
            "site": "boomtick.blog", "repo": "arii/boomtick",
            "at": "2026-11-04T00:00:00Z",
            "filing": {"number": 12, "url": "https://u",
                       "repo": "arii/boomtick"},
            "groups": [{
                "category": "Accessibility",
                "title": "Accessibility (1 finding(s), worst: high)",
                "findings": [{
                    "severity": "high",
                    "title": "[P1] image without alt text",
                    "route": "/", "viewport": "static",
                    "evidence": "src=x.png",
                    "category": "Accessibility"}]}]}
        with open(os.path.join(self._state_tmp.name,
                               "boomtick.blog.json"),
                  "w", encoding="utf-8") as f:
            json_lib.dump(report, f)
        resp = self.client.get(
            "/api/jobs/ux-audit/groups?site=boomtick.blog")
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["site"], "boomtick.blog")
        self.assertEqual(len(payload["groups"]), 1)
        group = payload["groups"][0]
        self.assertEqual(group["index"], 1)
        self.assertEqual(group["worst"], "high")
        self.assertIn("arii/boomtick#12", group["body"])
        self.assertIn("Target Files", group["body"])


class ImpactPassActionTest(_LedgerTmpDir, unittest.TestCase):
    """impact-pass as a first-class jobs action (#869)."""

    def setUp(self):
        import backend.api.routers.jobs as jobs_router

        self._ledger_setup()
        self._jobs_router = jobs_router
        self._old_registry = jobs_router.registry
        jobs_router.registry = RunRegistry()
        from backend.server import app

        self.client = TestClient(app)

    def tearDown(self):
        self._jobs_router.registry = self._old_registry
        self._ledger_teardown()

    def test_registered_not_destructive_not_repo_scoped(self):
        self.assertIn("impact-pass", engine.ALLOWED_ACTIONS)
        self.assertNotIn("impact-pass", engine.DESTRUCTIVE_ACTIONS)
        # Console-wide, not repo-scoped: the console is one target.
        self.assertNotIn("impact-pass", engine.REPO_SCOPED_ACTIONS)
        # Two captures x 600s worst case, like the ux-audit budget.
        self.assertEqual(engine.step_timeout("impact-pass"), 1200)

    def test_steps_run_local_runner(self):
        steps, err = engine.action_steps(
            "impact-pass", "remote-server", "", "")
        self.assertIsNone(err)
        self.assertEqual(
            steps, [[sys.executable, "-m", "backend.impact_pass"]])
        # -m steps execute FROM the deployed tree (step_cwd), so the
        # pass reads/writes the same state the API serves.
        self.assertEqual(engine.step_cwd(steps), engine.ENGINE_ROOT)

    def test_schedulable_through_action_schedules(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sched.json")
            old = os.environ.get("BUTTONS_JOBS_SCHEDULE_PATH")
            os.environ["BUTTONS_JOBS_SCHEDULE_PATH"] = path
            try:
                with mock.patch(
                    "backend.api.routers.issues._ensure_schedule_ticker"
                ):
                    resp = self.client.post(
                        "/api/jobs/schedule",
                        json={"action": "impact-pass",
                              "cadence": "daily"})
                    self.assertEqual(resp.status_code, 200)
                    sched = resp.json()["schedule"]
                    self.assertEqual(sched["action"], "impact-pass")
            finally:
                if old is None:
                    os.environ.pop("BUTTONS_JOBS_SCHEDULE_PATH", None)
                else:
                    os.environ["BUTTONS_JOBS_SCHEDULE_PATH"] = old


class ImpactPassRunnerTest(_LedgerTmpDir, unittest.TestCase):
    """Scheduled pass: seed, silence, findings, errors (#869)."""

    def setUp(self):
        import io
        import json
        import tempfile

        self._ledger_setup()
        self._tmp = tempfile.TemporaryDirectory()
        self.checkout = os.path.join(self._tmp.name, "checkout")
        os.makedirs(self.checkout)
        self.state_root = os.path.join(self._tmp.name, "impact-pass")
        self._old_state = os.environ.get("IMPACT_PASS_STATE_DIR")
        self._old_checkout = os.environ.get("IMPACT_PASS_CHECKOUT")
        self._old_routes = os.environ.get("IMPACT_PASS_ROUTES")
        self._old_path = os.environ.get("PATH", "")
        self._old_browsers = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        os.environ["IMPACT_PASS_STATE_DIR"] = self.state_root
        os.environ["IMPACT_PASS_CHECKOUT"] = self.checkout
        os.environ["IMPACT_PASS_ROUTES"] = "/:desktop"
        self._fixture_toolchain()
        deep_agent = os.path.join(engine.ENGINE_ROOT, "deep-agent")
        if deep_agent not in sys.path:
            sys.path.insert(0, deep_agent)
        import impact

        from backend import ledger
        from backend import impact_pass

        self.impact = impact
        self.ledger = ledger
        self.runner = impact_pass
        self._json = json
        self._io = io
        self.fixture_doc = {"route": "/", "viewport": "desktop",
                            "overflow": [],
                            "accessibility": {"violations": []},
                            "images": [], "tapTargets": []}
        self.fixture_png = impact.png_encode_rgb(
            2, 2, [bytes(6), bytes(6)])
        self.capture_ok = True
        self._run_patch = mock.patch("subprocess.run",
                                     side_effect=self._fake_td)
        self._run_patch.start()

    def tearDown(self):
        self._run_patch.stop()
        for key, old in (("IMPACT_PASS_STATE_DIR", self._old_state),
                         ("IMPACT_PASS_CHECKOUT", self._old_checkout),
                         ("IMPACT_PASS_ROUTES", self._old_routes),
                         ("PLAYWRIGHT_BROWSERS_PATH", self._old_browsers)):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        os.environ["PATH"] = self._old_path
        self._tmp.cleanup()
        self._ledger_teardown()

    def _fixture_toolchain(self):
        """Stub the #881 preflight inputs (bins + scripts + browser).

        The check logic itself is real and unmocked; only its inputs
        are fixtures, so these tests prove the gate trips exactly the
        shipped probe (not a mock of it).
        """
        from backend import impact_pass

        bindir = os.path.join(self._tmp.name, "bin")
        os.makedirs(bindir, exist_ok=True)
        for tool in impact_pass.TOOL_BINS:
            stub = os.path.join(bindir, tool)
            with open(stub, "w", encoding="utf-8") as f:
                f.write("#!/bin/sh\nexit 0\n")
            os.chmod(stub, 0o755)
        # Fixture bindir ONLY (not prepended): missing-tool cases
        # must fail deterministically even where the ambient PATH
        # carries a real td/node/pnpm. Nothing in these tests execs
        # (subprocess is mocked), so no system tool is needed.
        os.environ["PATH"] = bindir
        for rel in impact_pass.UX_SCRIPTS:
            full = os.path.join(self.checkout, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write("// fixture\n")
        uxbin = os.path.join(self.checkout, "node_modules", ".bin")
        os.makedirs(uxbin, exist_ok=True)
        for tool in impact_pass.UX_BINS:
            stub = os.path.join(uxbin, tool)
            with open(stub, "w", encoding="utf-8") as f:
                f.write("#!/bin/sh\nexit 0\n")
            os.chmod(stub, 0o755)
        browsers = os.path.join(self._tmp.name, "ms-playwright")
        chrome = os.path.join(browsers, "chromium-9999",
                              "chrome-linux64", "chrome")
        os.makedirs(os.path.dirname(chrome), exist_ok=True)
        with open(chrome, "w", encoding="utf-8") as f:
            f.write("fixture\n")
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browsers
        self._bindir = bindir

    def _fake_td(self, argv, **kwargs):
        # Stubbed td ux subprocess (gh-stub style): materialize one
        # route+viewport capture, then report success.
        if not self.capture_ok:
            return FakeProc(1, "", "td ux exploded")
        checkout = kwargs.get("cwd", "")
        base = os.path.join(checkout, "artifacts", "ux-audit")
        results = os.path.join(base, "results")
        shots = os.path.join(base, "screenshots")
        os.makedirs(results, exist_ok=True)
        os.makedirs(shots, exist_ok=True)
        with open(os.path.join(results, "root-desktop.json"),
                  "w", encoding="utf-8") as f:
            self._json.dump(self.fixture_doc, f)
        with open(os.path.join(shots, "root-desktop.png"),
                  "wb") as f:
            f.write(self.fixture_png)
        return FakeProc(
            0, self._json.dumps({"status": "success", "results": []}),
            "")

    def _run_main(self):
        buf = self._io.StringIO()
        with mock.patch("sys.stdout", buf):
            code = self.runner.main([])
        return code, buf.getvalue()

    def _ux_rows(self):
        return self.ledger.read(kind="ux-finding", limit=50)

    def test_first_run_seeds_baseline_without_findings(self):
        code, out = self._run_main()
        self.assertEqual(code, 0)
        self.assertIn("seeded", out)
        baseline = os.path.join(self.state_root, "root-desktop",
                                "baseline", "results",
                                "root-desktop.json")
        self.assertTrue(os.path.isfile(baseline))
        self.assertEqual(self._ux_rows(), [])

    def test_no_change_run_is_silent(self):
        code, _ = self._run_main()
        self.assertEqual(code, 0)
        code, out = self._run_main()
        self.assertEqual(code, 0)
        self.assertIn("findings=0", out)
        self.assertEqual(self._ux_rows(), [])

    def test_changed_route_ledgers_findings_with_severity(self):
        code, _ = self._run_main()
        self.assertEqual(code, 0)
        self.fixture_doc = {**self.fixture_doc, "overflow": [
            {"tagName": "div", "id": "hero", "className": "wide",
             "rect": {"left": 0, "right": 2000, "width": 2000}}]}
        code, out = self._run_main()
        self.assertEqual(code, 0)
        self.assertIn("findings=1", out)
        rows = self._ux_rows()
        self.assertEqual(len(rows), 1)
        hit = rows[0]
        self.assertEqual(hit["kind"], "ux-finding")
        self.assertEqual(hit["source"], "impact-pass")
        self.assertEqual(hit["severity"], "high")
        self.assertEqual(hit["title"],
                         "1 new layout-overflow issue(s)")
        self.assertEqual(hit["route"], "/")
        self.assertEqual(hit["viewport"], "desktop")
        self.assertIn("<div>", hit["evidence"])
        self.assertIsInstance(hit["ts"], float)
        # Rolling baseline: a third identical fire is silent again.
        code, out = self._run_main()
        self.assertEqual(code, 0)
        self.assertIn("findings=0", out)
        self.assertEqual(len(self._ux_rows()), 1)

    def test_capture_error_fails_the_run(self):
        self.capture_ok = False
        code, out = self._run_main()
        self.assertEqual(code, 1)
        self.assertIn("error", out)
        self.assertFalse(os.path.isdir(self.state_root))
        self.assertEqual(self._ux_rows(), [])

    def test_bad_route_config_fails_fast(self):
        os.environ["IMPACT_PASS_ROUTES"] = "nope"
        code, out = self._run_main()
        self.assertEqual(code, 1)
        self.assertIn("config error", out)

    def _run_check(self):
        buf = self._io.StringIO()
        with mock.patch("sys.stdout", buf):
            code = self.runner.main(["--check"])
        return code, buf.getvalue()

    def test_check_reports_ok_on_full_toolchain(self):
        code, out = self._run_check()
        self.assertEqual(code, 0)
        self.assertIn("toolchain ok", out)
        self.assertNotIn("MISSING", out)

    def test_missing_tool_fails_with_actionable_message(self):
        # The #881 production failure (pnpm absent) must surface as a
        # named missing dep, not a cryptic td error.
        os.remove(os.path.join(self._bindir, "pnpm"))
        code, out = self._run_main()
        self.assertEqual(code, 1)
        self.assertIn("toolchain missing", out)
        self.assertIn("pnpm", out)
        self.assertIn("runbook", out)
        self.assertEqual(self._ux_rows(), [])

    def test_check_flag_names_each_gap(self):
        os.remove(os.path.join(self._bindir, "pnpm"))
        code, out = self._run_check()
        self.assertEqual(code, 1)
        self.assertIn("MISSING pnpm", out)

    def test_missing_scripts_fail_the_run(self):
        os.remove(os.path.join(
            self.checkout, "scripts", "ux-audit-runner.ts"))
        code, out = self._run_main()
        self.assertEqual(code, 1)
        self.assertIn("ux-audit-runner.ts", out)

    def test_missing_ux_bins_fail_the_run(self):
        os.remove(os.path.join(
            self.checkout, "node_modules", ".bin", "tsx"))
        code, out = self._run_main()
        self.assertEqual(code, 1)
        self.assertIn("tsx", out)

    def test_missing_browser_fails_the_run(self):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(
            self._tmp.name, "no-browsers-here")
        code, out = self._run_main()
        self.assertEqual(code, 1)
        self.assertIn("chromium", out)

    def test_resultless_capture_fails_instead_of_seeding_empty(self):
        # td reports success even when the runners wrote nothing
        # (console down): the pass must fail loud, never seed a
        # vacuous baseline that would stay silent forever.
        real_fake = self._fake_td

        def bare(argv, **kwargs):
            if not self.capture_ok:
                return real_fake(argv, **kwargs)
            base = os.path.join(kwargs.get("cwd", ""), "artifacts",
                                "ux-audit")
            os.makedirs(base, exist_ok=True)
            with open(os.path.join(base, "routes.json"), "w",
                      encoding="utf-8") as f:
                self._json.dump({"routes": ["/"]}, f)
            return FakeProc(
                0, self._json.dumps({"status": "success",
                                     "results": []}), "")
        # Seed first: the bare capture must fail even with a
        # previous fire's artifacts sitting in the checkout (td
        # never cleans them; the pass must not trust stale files).
        code, _ = self._run_main()
        self.assertEqual(code, 0)
        self._run_patch.stop()
        self._run_patch = mock.patch("subprocess.run",
                                     side_effect=bare)
        self._run_patch.start()
        try:
            code, out = self._run_main()
        finally:
            self._run_patch.stop()
            self._run_patch = mock.patch(
                "subprocess.run", side_effect=self._fake_td)
            self._run_patch.start()
        self.assertEqual(code, 1)
        self.assertIn("no results", out)
        # The failed fire leaves the seeded baseline untouched.
        self.assertTrue(os.path.isfile(os.path.join(
            self.state_root, "root-desktop", "baseline",
            "results", "root-desktop.json")))


class ReviewPrActionTest(_LedgerTmpDir, unittest.TestCase):
    """review-pr as a first-class jobs action (#886)."""

    def setUp(self):
        import backend.api.routers.jobs as jobs_router

        self._ledger_setup()
        self._jobs_router = jobs_router
        self._old_registry = jobs_router.registry
        jobs_router.registry = RunRegistry()
        from backend.server import app

        self.client = TestClient(app)

    def tearDown(self):
        self._jobs_router.registry = self._old_registry
        self._ledger_teardown()

    def test_registered_repo_scoped_not_destructive(self):
        self.assertIn("review-pr", engine.ALLOWED_ACTIONS)
        self.assertIn("review-pr", engine.REPO_SCOPED_ACTIONS)
        self.assertNotIn("review-pr", engine.DESTRUCTIVE_ACTIONS)
        # The --post path runs model synthesis, like triage-fanout.
        self.assertEqual(engine.step_timeout("review-pr"), 1200)

    def test_argv_validation(self):
        self.assertTrue(engine.validate_review_pr_argv(
            ["review-pr", "12"]))
        self.assertTrue(engine.validate_review_pr_argv(
            ["review-pr", "12", "--post"]))
        self.assertFalse(engine.validate_review_pr_argv(
            ["review-pr"]))
        self.assertFalse(engine.validate_review_pr_argv(
            ["review-pr", "0"]))
        self.assertFalse(engine.validate_review_pr_argv(
            ["review-pr", "-3"]))
        self.assertFalse(engine.validate_review_pr_argv(
            ["review-pr", "12", "--dry-run"]))
        self.assertFalse(engine.validate_review_pr_argv(
            ["review-pr", "12; rm -rf /"]))
        self.assertFalse(engine.validate_review_pr_argv(
            ["run-td", "12"]))

    def test_steps_dry_run_by_default(self):
        steps, err = engine.action_steps(
            "review-pr", "remote-server", "arii/remote-server",
            "/srv/remote-server", ["review-pr", "12"])
        self.assertIsNone(err)
        self.assertEqual(
            steps, [[sys.executable, "-m", "backend.review",
                     "--repo", "arii/remote-server", "--pr", "12",
                     "--dry-run"]])
        # -m steps execute FROM the deployed tree (step_cwd).
        self.assertEqual(engine.step_cwd(steps), engine.ENGINE_ROOT)

    def test_steps_post_only_when_explicit(self):
        steps, err = engine.action_steps(
            "review-pr", "remote-server", "arii/remote-server",
            "/srv/remote-server", ["review-pr", "12", "--post"])
        self.assertIsNone(err)
        self.assertEqual(steps[0][-1], "--post")
        self.assertNotIn("--dry-run", steps[0])

    def test_steps_reject_missing_repo_and_argv(self):
        steps, err = engine.action_steps(
            "review-pr", "remote-server", "", "", ["review-pr", "12"])
        self.assertIsNone(steps)
        self.assertIn("needs a repo", err)
        steps, err = engine.action_steps(
            "review-pr", "remote-server", "arii/remote-server",
            "/srv/remote-server", None)
        self.assertIsNone(steps)
        self.assertIn("review-pr", err)
        steps, err = engine.action_steps(
            "review-pr", "remote-server", "arii/remote-server",
            "/srv/remote-server", ["review-pr", "x;evil"])
        self.assertIsNone(steps)

    def test_dispatch_dry_runs_by_default(self):
        import backend.api.routers.jobs as jobs_router

        with mock.patch.object(jobs_router, "_launch") as launched, \
                mock.patch("backend.core.get_repo_status",
                           return_value={"repo": "remote-server"}):
            resp = self.client.post(
                "/api/jobs/run-review",
                json={"repo": "remote-server", "pr": 12})
            self.assertEqual(resp.status_code, 200)
            rid = resp.json()["id"]
            launched.assert_called_once_with(
                rid, "review-pr", "remote-server",
                td_argv=["review-pr", "12"])
            # Queue (#1025): a second dispatch takes the free
            # slot instead of conflicting.
            resp2 = self.client.post(
                "/api/jobs/run-review",
                json={"repo": "remote-server", "pr": 13})
            self.assertEqual(resp2.status_code, 200)
            self.assertFalse(resp2.json()["queued"])
            rid2 = resp2.json()["id"]
            row = next(j for j in
                       self.client.get("/api/jobs").json()["jobs"]
                       if j["id"] == rid)
            self.assertEqual(row["kind"], "review-pr")
            self.assertEqual(row["td_argv"], ["review-pr", "12"])
        jobs_router.registry.finish_run(rid, 0)
        jobs_router.registry.finish_run(rid2, 0)

    def test_dispatch_post_rides_explicit_flag(self):
        import backend.api.routers.jobs as jobs_router

        with mock.patch.object(jobs_router, "_launch") as launched, \
                mock.patch("backend.core.get_repo_status",
                           return_value={"repo": "remote-server"}):
            resp = self.client.post(
                "/api/jobs/run-review",
                json={"repo": "remote-server", "pr": 12,
                      "post": True})
            self.assertEqual(resp.status_code, 200)
            rid = resp.json()["id"]
            launched.assert_called_once_with(
                rid, "review-pr", "remote-server",
                td_argv=["review-pr", "12", "--post"])
        jobs_router.registry.finish_run(rid, 0)

    def test_dispatch_rejects_bad_repo_and_pr(self):
        import backend.api.routers.jobs as jobs_router

        with mock.patch.object(jobs_router, "_launch"), \
                mock.patch("backend.core.get_repo_status",
                           side_effect=ValueError("nope")):
            resp = self.client.post(
                "/api/jobs/run-review",
                json={"repo": "ghost", "pr": 12})
            self.assertEqual(resp.status_code, 400)
        with mock.patch.object(jobs_router, "_launch"), \
                mock.patch("backend.core.get_repo_status",
                           return_value={"repo": "remote-server"}):
            for pr in (0, -3):
                resp = self.client.post(
                    "/api/jobs/run-review",
                    json={"repo": "remote-server", "pr": pr})
                self.assertEqual(resp.status_code, 400, msg=pr)
            missing = self.client.post(
                "/api/jobs/run-review",
                json={"repo": "remote-server"})
            self.assertEqual(missing.status_code, 400)


class SuperviseActionTest(_LedgerTmpDir, unittest.TestCase):
    """supervise as a first-class jobs action (#886)."""

    def setUp(self):
        import tempfile

        import backend.api.routers.jobs as jobs_router

        self._ledger_setup()
        self._jobs_router = jobs_router
        self._old_registry = jobs_router.registry
        jobs_router.registry = RunRegistry()
        from backend.server import app

        self.client = TestClient(app)
        # Fake deep-agent tree: action_steps only stats the venv
        # python + script, so empty files keep this hermetic.
        self._agent_tmp = tempfile.TemporaryDirectory()
        venv_bin = os.path.join(self._agent_tmp.name, ".venv", "bin")
        os.makedirs(venv_bin)
        self.fake_python = os.path.join(venv_bin, "python")
        with open(self.fake_python, "w") as f:
            f.write("#!/bin/sh\n")
        self.fake_script = os.path.join(
            self._agent_tmp.name, "deep_chat.py")
        with open(self.fake_script, "w") as f:
            f.write("# fake\n")
        self._old_agent_dir = os.environ.get("DEEP_AGENT_DIR")
        self._old_agent_venv = os.environ.get("DEEP_AGENT_VENV")
        os.environ["DEEP_AGENT_DIR"] = self._agent_tmp.name
        os.environ.pop("DEEP_AGENT_VENV", None)

    def tearDown(self):
        self._jobs_router.registry = self._old_registry
        if self._old_agent_dir is None:
            os.environ.pop("DEEP_AGENT_DIR", None)
        else:
            os.environ["DEEP_AGENT_DIR"] = self._old_agent_dir
        if self._old_agent_venv is None:
            os.environ.pop("DEEP_AGENT_VENV", None)
        else:
            os.environ["DEEP_AGENT_VENV"] = self._old_agent_venv
        self._agent_tmp.cleanup()
        self._ledger_teardown()

    def test_registered_repo_scoped_not_destructive(self):
        self.assertIn("supervise", engine.ALLOWED_ACTIONS)
        self.assertIn("supervise", engine.REPO_SCOPED_ACTIONS)
        self.assertNotIn("supervise", engine.DESTRUCTIVE_ACTIONS)
        # An agent sweep over a checkout, like triage-fanout.
        self.assertEqual(engine.step_timeout("supervise"), 1200)

    def test_steps_run_supervise_mode(self):
        steps, err = engine.action_steps(
            "supervise", "remote-server", "arii/remote-server",
            "/srv/remote-server")
        self.assertIsNone(err)
        self.assertEqual(
            steps, [[self.fake_python, self.fake_script,
                     "arii/remote-server", "--supervise"]])

    def test_steps_need_repo_venv_and_script(self):
        steps, err = engine.action_steps(
            "supervise", "remote-server", "", "")
        self.assertIsNone(steps)
        self.assertIn("needs a repo", err)
        os.remove(self.fake_python)
        steps, err = engine.action_steps(
            "supervise", "remote-server", "arii/remote-server",
            "/srv/remote-server")
        self.assertIsNone(steps)
        self.assertIn("venv python", err)

    def test_dispatchable_from_jobs_run(self):
        import backend.api.routers.jobs as jobs_router

        with mock.patch.object(jobs_router, "_launch") as launched, \
                mock.patch("backend.core.get_repo_status",
                           return_value={"repo": "remote-server"}):
            resp = self.client.post(
                "/api/jobs/run",
                json={"action": "supervise",
                      "repo": "remote-server"})
            self.assertEqual(resp.status_code, 200)
            rid = resp.json()["id"]
            launched.assert_called_once_with(
                rid, "supervise", "remote-server", td_argv=None)
        jobs_router.registry.finish_run(rid, 0)


if __name__ == "__main__":
    unittest.main()
