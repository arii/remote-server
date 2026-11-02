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
        resp = self.client.get("/api/jobs")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"jobs": []})

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


if __name__ == "__main__":
    unittest.main()
