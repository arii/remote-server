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
        # Bad name -> 400.
        resp = self.client.get("/api/repo/status", params={"repo": "a/b/c"})
        self.assertEqual(resp.status_code, 400)
        # Unknown checkout -> 404.
        resp = self.client.get(
            "/api/repo/status", params={"repo": "no-such-repo-xyz"})
        self.assertEqual(resp.status_code, 404)
        # Real checkout honours BUTTONS_REPOS_DIR; fall back to this repo.
        os.environ.setdefault("BUTTONS_REPOS_DIR", "/tmp")
        from backend import core
        real = os.path.dirname(
            os.path.dirname(os.path.abspath(core.__file__)))
        os.environ["BUTTONS_REPOS_DIR"] = os.path.dirname(real)
        name = os.path.basename(real)
        resp = self.client.get("/api/repo/status", params={"repo": name})
        if resp.status_code == 200:
            body = resp.json()
            self.assertEqual(body["repo"], name)
            self.assertIn("branch", body)
            self.assertIn("short", body)
            self.assertIn("clean", body)

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

    def test_agent_sessions_list_shape(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        sess = os.path.join(tmp, "t1.jsonl")
        with open(sess, "w") as f:
            f.write('{"type": "session", "id": "s1", "cwd": "/srv/repos/demo",'
                    ' "timestamp": "2024-01-01"}\n')
            f.write('{"message": {"role": "user", "content": "hello world"}}\n')
        os.environ["PI_CODING_AGENT_SESSION_DIR"] = tmp
        try:
            resp = self.client.get("/api/agents/sessions")
        finally:
            del os.environ["PI_CODING_AGENT_SESSION_DIR"]
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("sessions", body)
        self.assertEqual(len(body["sessions"]), 1)
        row = body["sessions"][0]
        for field in ("file", "mtime", "cwd", "repo", "title"):
            self.assertIn(field, row)
        self.assertEqual(row["file"], "t1.jsonl")
        self.assertEqual(row["repo"], "demo")
        self.assertEqual(row["title"], "hello world")

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


if __name__ == "__main__":
    unittest.main()
