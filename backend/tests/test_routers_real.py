"""Real-endpoint tests: TestClient assertions for the read-only routers."""

import os
import unittest

try:
    from fastapi.testclient import TestClient

    from backend.server import app

    HAS_CLIENT = True
except ImportError:
    HAS_CLIENT = False


@unittest.skipUnless(HAS_CLIENT, "fastapi.testclient not importable")
class RealRoutersTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_system_health_shape(self):
        resp = self.client.get("/api/system/health")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        for section in ("disk", "memory"):
            self.assertIn(section, body)
            info = body[section]
            self.assertIn("summary", info)
            self.assertIn("percent", info)
        self.assertIsInstance(body["containers"], list)

    def test_repo_status_validated(self):
        import subprocess
        import tempfile

        # Bad name -> 400.
        resp = self.client.get("/api/repo/status", params={"repo": "a/b/c"})
        self.assertEqual(resp.status_code, 400)
        # Unknown checkout -> 404.
        resp = self.client.get(
            "/api/repo/status", params={"repo": "no-such-repo-xyz"})
        self.assertEqual(resp.status_code, 404)
        # Real checkout honours BUTTONS_REPOS_DIR.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "demo")
            os.makedirs(path)
            env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                   "GIT_COMMITTER_NAME": "t",
                   "GIT_COMMITTER_EMAIL": "t@t"}
            subprocess.run(["git", "init", "-q"], cwd=path, env=env,
                           check=True)
            old_repos_dir = os.environ.get("BUTTONS_REPOS_DIR")
            self.addCleanup(self._restore_repos_dir, old_repos_dir)
            os.environ["BUTTONS_REPOS_DIR"] = tmp
            resp = self.client.get(
                "/api/repo/status", params={"repo": "demo"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["repo"], "demo")
        self.assertIn("branch", body)
        self.assertIn("short", body)
        self.assertIn("clean", body)

    @staticmethod
    def _restore_repos_dir(old):
        if old is None:
            os.environ.pop("BUTTONS_REPOS_DIR", None)
        else:
            os.environ["BUTTONS_REPOS_DIR"] = old

    def test_system_stats_shape(self):
        resp = self.client.get("/api/system/stats")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIsInstance(body["containers"], list)
        for row in body["containers"]:
            for key in ("name", "service", "state", "cpu_pct",
                        "mem_used", "mem_limit"):
                self.assertIn(key, row)

    def test_jobs_honest_empty(self):
        import backend.api.routers.jobs as jobs_router
        from backend.exec.engine import RunRegistry

        jobs_router.registry = RunRegistry()
        resp = self.client.get("/api/jobs")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"jobs": []})

    def test_jobs_lists_runs(self):
        import backend.api.routers.jobs as jobs_router
        from backend.exec.engine import RunRegistry

        jobs_router.registry = RunRegistry()
        rid, err = jobs_router.registry.start_run("logs")
        self.assertIsNone(err)
        jobs_router.registry.finish_run(rid, 0)
        resp = self.client.get("/api/jobs")
        self.assertEqual(resp.status_code, 200)
        jobs = resp.json()["jobs"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], rid)
        self.assertEqual(jobs[0]["kind"], "logs")
        self.assertTrue(jobs[0]["done"])
        self.assertEqual(jobs[0]["exit_code"], 0)

    def test_agent_runs_shapes(self):
        from backend.api.routers import agents
        real_list = agents.runs.list_runs
        real_get = agents.runs.get_run
        agents.runs.list_runs = lambda limit=50: [
            {"id": "run-0", "script": "deep_chat.py",
             "thread": "abc", "model": "m", "ok": True}]
        agents.runs.get_run = lambda rid: (
            {"id": "run-0", "entry": {"thread_id": "abc"}, "trace": []}
            if rid == "run-0" else None)
        try:
            resp = self.client.get("/api/agents/runs")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["runs"][0]["id"], "run-0")
            resp = self.client.get("/api/agents/runs/run-0")
            self.assertEqual(resp.status_code, 200)
            self.assertIn("trace", resp.json())
            resp = self.client.get("/api/agents/runs/run-9")
            self.assertEqual(resp.status_code, 404)
        finally:
            agents.runs.list_runs = real_list
            agents.runs.get_run = real_get

    def test_delete_agent_run(self):
        import json
        import tempfile

        from backend.api.routers import agents
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ledger.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                for thread in ("t0", "t1"):
                    f.write(json.dumps(
                        {"kind": "deep-agent-run", "thread_id": thread,
                         "extra": {}}) + "\n")
            real_ledger = agents.runs.LEDGER
            agents.runs.LEDGER = path
            try:
                bad = self.client.request(
                    "DELETE", "/api/agents/runs/run-0",
                    json={"intent": "agents-delete"})
                self.assertEqual(bad.status_code, 400)
                resp = self.client.request(
                    "DELETE", "/api/agents/runs/run-0",
                    json={"intent": "agents-delete", "confirmed": True})
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json(), {"ok": True, "id": "run-0"})
                again = self.client.request(
                    "DELETE", "/api/agents/runs/run-0",
                    json={"intent": "agents-delete", "confirmed": True})
                self.assertEqual(again.status_code, 404)
                listed = self.client.get("/api/agents/runs")
                self.assertEqual(
                    [r["id"] for r in listed.json()["runs"]], ["run-1"])
            finally:
                agents.runs.LEDGER = real_ledger

    def test_agent_budget(self):
        import json
        import tempfile
        from unittest import mock

        from backend.api.routers import agents
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ledger.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps(
                    {"kind": "deep-agent-run",
                     "model": "nebius:zai-org/GLM-5.3-Flash",
                     "usage": {"input_tokens": 2000,
                               "output_tokens": 1000,
                               "total_tokens": 3000},
                     "cost_usd": 0.0008, "metered": True}) + "\n")
            real_ledger = agents.runs.LEDGER
            agents.runs.LEDGER = path
            try:
                with mock.patch.dict(
                        os.environ,
                        {"NEBIUS_SPEND_CAP_USD": "5.00"}, clear=True):
                    resp = self.client.get("/api/agents/budget")
                self.assertEqual(resp.status_code, 200)
                body = resp.json()
                self.assertEqual(body["cap_usd"], 5.0)
                self.assertAlmostEqual(body["spent_usd"], 0.0008)
                self.assertAlmostEqual(body["remaining_usd"], 4.9992)
                self.assertFalse(body["blocked"])
                self.assertEqual(body["unknown"], 0)
                with mock.patch.dict(os.environ, {}, clear=True):
                    resp = self.client.get("/api/agents/budget")
                body = resp.json()
                # #885: unset fails closed to the $5.00 default.
                self.assertEqual(body["cap_usd"], 5.0)
                self.assertAlmostEqual(body["remaining_usd"], 4.9992)
                self.assertFalse(body["blocked"])
                with mock.patch.dict(
                        os.environ, {"NEBIUS_SPEND_CAP_USD": "0"},
                        clear=True):
                    resp = self.client.get("/api/agents/budget")
                self.assertTrue(resp.json()["blocked"])
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(
                        {"kind": "deep-agent-run",
                         "model": "nebius:uncarded/future-9z",
                         "usage": {"input_tokens": 2000,
                                   "output_tokens": 1000,
                                   "total_tokens": 3000}}) + "\n")
                with mock.patch.dict(
                        os.environ,
                        {"NEBIUS_SPEND_CAP_USD": "5.00"}, clear=True):
                    resp = self.client.get("/api/agents/budget")
                body = resp.json()
                self.assertEqual(body["unknown"], 1)
                self.assertTrue(body["blocked"])
            finally:
                agents.runs.LEDGER = real_ledger

    def test_clear_agent_runs(self):
        import json
        import tempfile

        from backend.api.routers import agents
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ledger.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                for thread in ("t0", "t1"):
                    f.write(json.dumps(
                        {"kind": "deep-agent-run", "thread_id": thread,
                         "extra": {}}) + "\n")
            real_ledger = agents.runs.LEDGER
            agents.runs.LEDGER = path
            try:
                bad = self.client.post(
                    "/api/agents/runs/clear",
                    json={"intent": "agents-clear"})
                self.assertEqual(bad.status_code, 400)
                resp = self.client.post(
                    "/api/agents/runs/clear",
                    json={"intent": "agents-clear", "confirmed": True})
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json(), {"ok": True, "cleared": 2})
                listed = self.client.get("/api/agents/runs")
                self.assertEqual(listed.json(), {"runs": []})
            finally:
                agents.runs.LEDGER = real_ledger


if __name__ == "__main__":
    unittest.main()
