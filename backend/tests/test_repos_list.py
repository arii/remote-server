"""GET /api/repos: shape, multiple repos, unknown-dir handling."""

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routers import repo as repo_router


def _proc(rc=0, out=""):
    return SimpleNamespace(returncode=rc, stdout=out, stderr="")


def _fake_run_factory(origins=None, branches=None, dirty=None):
    origins = origins or {}
    branches = branches or {}
    dirty = dirty or set()

    def fake_run(cmd, **kwargs):
        path = cmd[cmd.index("-C") + 1]
        name = path.rstrip("/").rsplit("/", 1)[-1]
        if "remote.origin.url" in cmd:
            url = origins.get(name)
            if url is None:
                return _proc(rc=1, out="")
            return _proc(out=url + "\n")
        if "rev-parse" in cmd:
            return _proc(out=(branches.get(name, "main")) + "\n")
        if "status" in cmd:
            if name in dirty:
                return _proc(out="## main...origin/main\n M file.py\n")
            return _proc(out="## main...origin/main\n")
        raise AssertionError(f"unexpected git call: {cmd}")

    return fake_run


def _make_client(repos_dir, stack_dir, fake_run):
    app = FastAPI()
    app.include_router(repo_router.router)
    app.include_router(repo_router.repos_router)
    patches = [
        patch.object(repo_router, "list_repos",
                     wraps=__import__("backend.core", fromlist=["list_repos"]).list_repos),
    ]
    return app, patches, repos_dir, stack_dir, fake_run


class ReposListTest(unittest.TestCase):
    def _get(self, tmp, names_with_git, names_without_git=(),
             origins=None, branches=None, dirty=None,
             stack_with_git=False, missing_root=False):
        import os
        from pathlib import Path

        from backend.core import list_repos

        root = Path(tmp) / "repos"
        stack = Path(tmp) / "stack"
        if missing_root:
            pass
        else:
            root.mkdir(parents=True, exist_ok=True)
            for name in list(names_with_git) + list(names_without_git):
                d = root / name
                d.mkdir(parents=True, exist_ok=True)
                if name in names_with_git:
                    (d / ".git").mkdir(exist_ok=True)
        if stack_with_git:
            stack.mkdir(parents=True, exist_ok=True)
            (stack / ".git").mkdir(exist_ok=True)
        got = list_repos(repos_dir=root, stack_dir=stack,
                         run=_fake_run_factory(origins, branches, dirty))
        app = FastAPI()
        app.include_router(repo_router.router)
        app.include_router(repo_router.repos_router)
        with patch.object(repo_router, "list_repos", return_value=got):
            client = TestClient(app)
            resp = client.get("/api/repos")
        return resp, got, root, stack

    def test_shape(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            resp, got, _, _ = self._get(
                tmp, ["alpha"],
                origins={"alpha": "git@github.com:arii/alpha.git"},
                branches={"alpha": "main"})
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertIn("repos", body)
            self.assertEqual(len(body["repos"]), 1)
            entry = body["repos"][0]
            self.assertEqual(entry, {"name": "alpha", "full": "arii/alpha",
                                     "branch": "main", "clean": True})

    def test_multiple_repos_sorted_and_dirty_flag(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            resp, got, _, _ = self._get(
                tmp, ["zeta", "beta"],
                origins={"zeta": "https://github.com/arii/zeta",
                         "beta": "git@github.com:arii/beta.git"},
                branches={"zeta": "feat", "beta": "main"},
                dirty={"zeta"})
            body = resp.json()
            self.assertEqual([r["name"] for r in body["repos"]],
                             ["beta", "zeta"])
            by_name = {r["name"]: r for r in body["repos"]}
            self.assertTrue(by_name["beta"]["clean"])
            self.assertFalse(by_name["zeta"]["clean"])
            self.assertEqual(by_name["zeta"]["branch"], "feat")

    def test_unknown_dir_handling(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            # dir without .git skipped; unparseable origin keeps default full;
            # unsafe name skipped; missing root degrades to stack/empty.
            resp, got, root, stack = self._get(
                tmp, ["good", "noorigin"],
                names_without_git=["plain-dir"],
                origins={"good": "git@github.com:arii/good.git",
                         "noorigin": "not-a-remote"})
            names = [r["name"] for r in resp.json()["repos"]]
            self.assertIn("good", names)
            self.assertIn("noorigin", names)
            self.assertNotIn("plain-dir", names)
            by_name = {r["name"]: r for r in resp.json()["repos"]}
            self.assertEqual(by_name["noorigin"]["full"], "arii/noorigin")
            # unsafe names never reach git
            (root / "bad;name").mkdir(exist_ok=True)
            from backend.core import list_repos
            got2 = list_repos(repos_dir=root, stack_dir=stack,
                              run=_fake_run_factory(
                                  {"good": "git@github.com:arii/good.git",
                                   "noorigin": "not-a-remote"}))
            self.assertNotIn("bad;name", [r["name"] for r in got2])
            # missing repos root -> empty (no stack checkout here)
            resp2, got3, _, _ = self._get(
                tmp + "-missing", ["x"], missing_root=True)
            self.assertEqual(resp2.json(), {"repos": []})


class ReposDirFallbackTest(unittest.TestCase):
    def test_missing_configured_root_falls_through(self):
        from backend.core import get_repos_dir
        with patch.dict(os.environ, {"BUTTONS_REPOS_DIR": "/nonexistent-root-xyz"}):
            got = get_repos_dir()
        if Path("/srv/repos").is_dir():
            self.assertEqual(got, Path("/srv/repos"))
        else:
            self.assertEqual(got, Path("/nonexistent-root-xyz"))


if __name__ == "__main__":
    unittest.main()
