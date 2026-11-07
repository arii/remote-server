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
                                     "branch": "main", "clean": True,
                                     "ahead": 0, "behind": 0,
                                     "status_count": 0})

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

    def test_behind_ahead_and_status_count(self):
        import tempfile
        from pathlib import Path

        from backend.core import list_repos

        def fake_run(cmd, **kwargs):
            path = cmd[cmd.index("-C") + 1]
            name = path.rstrip("/").rsplit("/", 1)[-1]
            if "remote.origin.url" in cmd:
                return _proc(out=f"git@github.com:arii/{name}.git\n")
            if "rev-parse" in cmd:
                return _proc(out="main\n")
            if "status" in cmd:
                if name == "laggy":
                    return _proc(
                        out="## main...origin/main [behind 3]\n"
                            " M a.py\n?? b.txt\n")
                if name == "aheady":
                    return _proc(
                        out="## main...origin/main [ahead 2, behind 1]\n")
                return _proc(out="## main...origin/main\n")
            raise AssertionError(f"unexpected git call: {cmd}")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repos"
            stack = Path(tmp) / "stack"
            for name in ("laggy", "aheady", "clean"):
                d = root / name
                d.mkdir(parents=True)
                (d / ".git").mkdir()
            got = list_repos(repos_dir=root, stack_dir=stack, run=fake_run)
            by_name = {r["name"]: r for r in got}
            self.assertEqual(by_name["laggy"]["behind"], 3)
            self.assertEqual(by_name["laggy"]["ahead"], 0)
            self.assertEqual(by_name["laggy"]["status_count"], 2)
            self.assertFalse(by_name["laggy"]["clean"])
            self.assertEqual(by_name["aheady"]["ahead"], 2)
            self.assertEqual(by_name["aheady"]["behind"], 1)
            self.assertEqual(by_name["aheady"]["status_count"], 0)
            self.assertTrue(by_name["aheady"]["clean"])
            self.assertEqual(by_name["clean"]["behind"], 0)

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


class ValidateActionBranchTest(unittest.TestCase):
    """Optional action branch (#708): blank is absent, otherwise the
    name must be well-formed and exist in the checkout."""

    def _checkout(self, tmp):
        root = Path(tmp)
        (root / "demo" / ".git").mkdir(parents=True)
        return root

    def test_blank_is_absent_without_touching_git(self):
        from backend.core import validate_action_branch

        def boom(cmd, **kwargs):
            raise AssertionError(f"git must not run: {cmd}")

        with patch.dict(os.environ, {"BUTTONS_REPOS_DIR": "/nonexistent-root-xyz"}):
            self.assertEqual(validate_action_branch("demo", "", run=boom), "")
            self.assertEqual(validate_action_branch("demo", "   ", run=boom), "")
            self.assertEqual(validate_action_branch("demo", None, run=boom), "")

    def test_malformed_names_rejected(self):
        import tempfile

        from backend.core import validate_action_branch

        def boom(cmd, **kwargs):
            raise AssertionError(f"git must not run: {cmd}")

        with tempfile.TemporaryDirectory() as tmp:
            root = self._checkout(tmp)
            with patch.dict(os.environ, {"BUTTONS_REPOS_DIR": str(root)}):
                for bad in ("../main", "-main", "a..b", "a//b",
                            "a.lock", "main.lock", "a b", "a@{b}",
                            True, 5, ["main"]):
                    with self.assertRaises(ValueError, msg=repr(bad)):
                        validate_action_branch("demo", bad, run=boom)

    def test_unknown_branch_rejected(self):
        import tempfile

        from backend.core import validate_action_branch

        def missing(cmd, **kwargs):
            self.assertIn("refs/heads/gone", cmd)
            return _proc(rc=1, out="")

        with tempfile.TemporaryDirectory() as tmp:
            root = self._checkout(tmp)
            with patch.dict(os.environ, {"BUTTONS_REPOS_DIR": str(root)}):
                with self.assertRaises(ValueError) as ctx:
                    validate_action_branch("demo", "gone", run=missing)
                self.assertIn("unknown branch", str(ctx.exception))

    def test_known_branch_returned_stripped(self):
        import tempfile

        from backend.core import validate_action_branch

        seen = []

        def present(cmd, **kwargs):
            seen.append(cmd)
            self.assertIn("refs/heads/feat/x", cmd)
            return _proc(rc=0, out="abc123\n")

        with tempfile.TemporaryDirectory() as tmp:
            root = self._checkout(tmp)
            with patch.dict(os.environ, {"BUTTONS_REPOS_DIR": str(root)}):
                self.assertEqual(
                    validate_action_branch("demo", "  feat/x ", run=present),
                    "feat/x")
                self.assertTrue(seen)

    def test_missing_checkout_reads_as_unknown_branch(self):
        import tempfile

        from backend.core import validate_action_branch

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"BUTTONS_REPOS_DIR": tmp}):
                with self.assertRaises(ValueError) as ctx:
                    validate_action_branch("demo", "main", run=_proc)
                self.assertIn("unknown branch", str(ctx.exception))

    def test_HEAD_means_current_checkout(self):
        # Detached-HEAD checkouts (CI) report the branch as HEAD, which
        # never exists under refs/heads — treat as omitted, not unknown
        # (e2e jobs-run 400d "unknown branch: 'HEAD'" on every CI run).
        from backend.core import validate_action_branch

        def boom(cmd, **kwargs):
            raise AssertionError(f"git must not run: {cmd}")

        with patch.dict(os.environ, {"BUTTONS_REPOS_DIR": "/nonexistent-root-xyz"}):
            self.assertEqual(validate_action_branch("demo", "HEAD", run=boom), "")


if __name__ == "__main__":
    unittest.main()
