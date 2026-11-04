"""Tests for the jobs + execution port (stdlib unittest only).

Subprocess is mocked except for the watchdog tests, which must run a
real hung process (a short python sleep) to prove the kill.
"""

import asyncio
import inspect
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
        self.assertEqual(reg.active_id, active_id)
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

    def test_second_start_carries_active_id(self):
        reg = RunRegistry()
        rid, err = reg.start_run("logs")
        self.assertIsNone(err)
        rid2, err2 = reg.start_run("logs")
        self.assertIsNone(rid2)
        self.assertIn("already in progress", err2)
        self.assertIn(rid, err2)
        reg.finish_run(rid, 0)
        rid3, err3 = reg.start_run("logs")
        self.assertIsNone(err3)
        self.assertIsNotNone(rid3)


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
            "backend.api.routers.jobs.td_run_async",
            new=mock.AsyncMock(return_value='{"plan_path": "p.md"}'),
        ) as td_mock:
            resp = self.client.post(
                "/api/jobs/run-plan", json={"plan": "issue-audit"}
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["plan_path"], "p.md")
        self.assertEqual(td_mock.await_count, 1)
        self.assertEqual(
            td_mock.await_args[0][0],
            "td --json agent plan-issue-audit --all-open")

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
        # Endpoint pins #512: clear-all refuses with 409 naming the
        # still-visible active run; post-clear conflicts never name
        # an invisible run.
        import backend.api.routers.jobs as jobs_router

        jobs_router.registry = RunRegistry()
        rid, err = jobs_router.registry.start_run("logs")
        self.assertIsNone(err)
        try:
            resp = self.client.post(
                "/api/jobs/clear",
                json={"all": True, "intent": "jobs-clear",
                      "confirmed": True})
            self.assertEqual(resp.status_code, 409)
            self.assertIn(rid, resp.json()["detail"])
            listed = self.client.get("/api/jobs").json()["jobs"]
            self.assertEqual([j["id"] for j in listed], [rid])
            resp2 = self.client.post(
                "/api/jobs/run", json={"action": "logs"})
            self.assertEqual(resp2.status_code, 409)
            self.assertIn(rid, resp2.json()["detail"])
        finally:
            jobs_router.registry.finish_run(rid, 0)
        resp3 = self.client.post(
            "/api/jobs/clear",
            json={"all": True, "intent": "jobs-clear",
                  "confirmed": True})
        self.assertEqual(resp3.json(), {"ok": True, "cleared": 1})

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
            launched.assert_called_once_with(
                resp.json()["id"], "recreate", "remote-server")
        jobs_router.registry.finish_run(resp.json()["id"], 0)

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
        # No-ops bump nothing: missing append, busy start, empty clear.
        reg.append("deadbeef0000", "x")
        self.assertEqual(reg._version, 3)
        reg2 = RunRegistry()
        reg2.start_run("logs")
        self.assertIsNone(reg2.start_run("logs")[0])
        self.assertEqual(reg2._version, 1)
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


def _sleep_step(seconds):
    return [sys.executable, "-c",
            f"import time;time.sleep({seconds})"]


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
        self.assertIsNone(reg.active_id)

    def test_normal_step_unaffected(self):
        chunks = []
        code = engine.run_step_pipe(
            [sys.executable, "-c", "print('hi')"], chunks.append)
        self.assertEqual(code, 0)
        self.assertIn("hi", "".join(chunks))


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
        self.assertIsNone(reg.active_id)

    def test_finish_run_clears_missing_record(self):
        reg = RunRegistry()
        reg.active_id = "deadbeefcafe"
        reg.finish_run("deadbeefcafe", 1)
        self.assertIsNone(reg.active_id)

    def test_launch_failure_releases_lock(self):
        import backend.api.routers.jobs as jobs_router
        from backend.server import app

        from fastapi.testclient import TestClient

        client = TestClient(app, raise_server_exceptions=False)
        for path, payload in (
            ("/api/jobs/run", {"action": "logs"}),
            ("/api/jobs/run-td", {"command": "td context-warm"}),
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
            self.assertIsNone(jobs_router.registry.active_id)
            rid, err = jobs_router.registry.start_run("logs")
            self.assertIsNone(err)
            jobs_router.registry.finish_run(rid, 0)


class AsyncPlanTest(unittest.TestCase):
    def test_run_plan_route_is_coroutine(self):
        import backend.api.routers.jobs as jobs_router

        self.assertTrue(
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
        self.assertIsNone(reg.active_id)
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
        self.assertIsNone(reg.active_id)


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

        def _spy(step, append, runner=None, timeout=None, on_proc=None):
            seen["timeout"] = timeout
            return real_pipe(step, append, runner=runner,
                             timeout=timeout, on_proc=on_proc)

        with mock.patch.object(engine, "run_step_pipe",
                               side_effect=_spy):
            engine.execute_run(
                reg, rid, "triage-fanout", "boomtick", "arii/boomtick",
                "/srv/repos/boomtick",
                runner=lambda argv: (0, "fanout: done\n"))
        self.assertEqual(seen["timeout"], 1200)
        self.assertTrue(reg.runs[rid]["done"])
        self.assertEqual(reg.runs[rid]["exit_code"], 0)

    def test_launch_single_flight_rerun_cancel(self):
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
            # Single-flight: a second launch conflicts while active.
            resp2 = self.client.post(
                "/api/jobs/run",
                json={"action": "triage-fanout", "repo": "boomtick"})
            self.assertEqual(resp2.status_code, 409)
            # History row carries the action + repo.
            rows = [j for j in self.client.get("/api/jobs").json()["jobs"]
                    if j["id"] == rid]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["kind"], "triage-fanout")
            self.assertEqual(rows[0]["repo"], "boomtick")
            # Cancel works on the active fanout run.
            resp3 = self.client.post(f"/api/jobs/runs/{rid}/cancel")
            self.assertEqual(resp3.status_code, 202)
            jobs_router.registry.finish_run(rid, 130)
            # Re-run replays action+repo like any plain action.
            resp4 = self.client.post(
                "/api/jobs/run",
                json={"action": "triage-fanout", "repo": "boomtick"})
            self.assertEqual(resp4.status_code, 200)
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
        self.assertIsNone(boot.active_id)
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


if __name__ == "__main__":
    unittest.main()
