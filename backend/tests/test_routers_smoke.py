"""Smoke test for backend routers (stdlib unittest only)."""

import unittest

try:
    from fastapi.testclient import TestClient

    from backend.server import app

    HAS_CLIENT = True
except ImportError:
    HAS_CLIENT = False


class RouterSmokeTest(unittest.TestCase):
    def test_routes_registered(self):
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        for name in ("agents", "repo", "system", "jobs"):
            src = (root / "api" / "routers" / f"{name}.py").read_text()
            tree = ast.parse(src)
            prefixes = [
                n.value
                for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            ]
            self.assertIn(f"/api/{name}", prefixes, name)
            self.assertIn("/health", prefixes, name)
            self.assertIn('{"ok": True}', src.replace('{"ok": True}', '{"ok": True}'))
            self.assertIn('"ok"', src)
        server_src = (root / "server.py").read_text()
        for name in ("agents", "repo", "system", "jobs"):
            self.assertIn(name, server_src)
        self.assertIn("FastAPI", server_src)

    def test_health_endpoints(self):
        if not HAS_CLIENT:
            self.skipTest("fastapi.testclient not importable")
        client = TestClient(app)
        for path in (
            "/health",
            "/api/agents/health",
            "/api/repo/health",
            "/api/system/health",
            "/api/jobs/health",
        ):
            resp = client.get(path)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json(), {"ok": True})

    def test_core_routines(self):
        from backend.core import resolve_repo_path, to_json

        self.assertEqual(to_json({"b": 1, "a": 2}), '{"a": 2, "b": 1}')
        self.assertTrue(str(resolve_repo_path("arii/tech-dancer")).endswith("tech-dancer"))


if __name__ == "__main__":
    unittest.main()
