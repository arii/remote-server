"""GET /api/repo/diff: status + diff stat + truncated full diff (#663)."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from backend.core import DIFF_CAP, get_repo_diff


def _proc(rc=0, out="", err=""):
    return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def _fake_run_factory(branch="main", short="", stat="", diff=""):
    def fake_run(cmd, **kwargs):
        if "rev-parse" in cmd:
            return _proc(out=branch + "\n")
        if cmd[-2:] == ["status", "--short"] or "status" in cmd:
            return _proc(out=short)
        if "diff" in cmd and "--stat" in cmd:
            return _proc(out=stat)
        if "diff" in cmd:
            return _proc(out=diff)
        raise AssertionError(f"unexpected git call: {cmd}")

    return fake_run


class RepoDiffCoreTest(unittest.TestCase):
    def _diff(self, tmp, **kwargs):
        path = os.path.join(tmp, "demo")
        os.makedirs(os.path.join(path, ".git"))
        with mock.patch(
                "backend.core.get_repos_dir", return_value=Path(tmp)):
            return get_repo_diff("demo", run=_fake_run_factory(**kwargs))

    def test_clean_repo_empty_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = self._diff(tmp)
        self.assertEqual(body["repo"], "demo")
        self.assertEqual(body["branch"], "main")
        self.assertTrue(body["clean"])
        self.assertEqual(body["status_short"], "")
        self.assertEqual(body["diff_stat"], "")
        self.assertEqual(body["diff"], "")
        self.assertFalse(body["truncated"])

    def test_dirty_repo_carries_status_and_diff(self):
        short = " M a.py\n?? b.py\n"
        stat = " a.py | 2 +-\n 1 file changed\n"
        diff = "diff --git a/a.py b/a.py\n+x\n"
        with tempfile.TemporaryDirectory() as tmp:
            body = self._diff(tmp, short=short, stat=stat, diff=diff)
        self.assertFalse(body["clean"])
        self.assertEqual(body["status_short"], short)
        self.assertEqual(body["diff_stat"], stat)
        self.assertEqual(body["diff"], diff)
        self.assertFalse(body["truncated"])

    def test_long_diff_truncates_at_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = self._diff(tmp, short=" M big.py\n",
                              diff="x" * (DIFF_CAP + 100))
        self.assertEqual(len(body["diff"]), DIFF_CAP)
        self.assertTrue(body["truncated"])

    def test_bad_name_raises_value_error(self):
        with self.assertRaises(ValueError):
            get_repo_diff("a/b/c", run=_fake_run_factory())

    def test_missing_checkout_raises_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                    "backend.core.get_repos_dir", return_value=Path(tmp)):
                with self.assertRaises(FileNotFoundError):
                    get_repo_diff("nope", run=_fake_run_factory())

    def test_git_failure_raises_runtime_error(self):
        def failing(cmd, **kwargs):
            if "rev-parse" in cmd:
                return _proc(out="main\n")
            return _proc(rc=128, err="fatal: boom")

        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "demo", ".git"))
            with mock.patch(
                    "backend.core.get_repos_dir", return_value=Path(tmp)):
                with self.assertRaises(RuntimeError):
                    get_repo_diff("demo", run=failing)


class RepoDiffRealGitTest(unittest.TestCase):
    def test_real_checkout_reports_real_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "demo")
            os.makedirs(path)
            env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                   "GIT_COMMITTER_NAME": "t",
                   "GIT_COMMITTER_EMAIL": "t@t"}
            subprocess.run(["git", "init", "-q"], cwd=path, env=env,
                           check=True)
            with open(os.path.join(path, "a.py"), "w") as fh:
                fh.write("x = 1\n")
            subprocess.run(["git", "add", "."], cwd=path, env=env,
                           check=True)
            subprocess.run(["git", "commit", "-qm", "one"], cwd=path,
                           env=env, check=True)
            with open(os.path.join(path, "a.py"), "a") as fh:
                fh.write("y = 2\n")
            with mock.patch(
                    "backend.core.get_repos_dir", return_value=Path(tmp)):
                body = get_repo_diff("demo")
        self.assertFalse(body["clean"])
        self.assertIn("a.py", body["status_short"])
        self.assertIn("a.py", body["diff_stat"])
        self.assertIn("y = 2", body["diff"])


class RepoDiffEndpointTest(unittest.TestCase):
    def setUp(self):
        from backend.server import app

        self.client = TestClient(app)

    def test_diff_validates_repo_name(self):
        resp = self.client.get("/api/repo/diff",
                               params={"repo": "a/b/c"})
        self.assertEqual(resp.status_code, 400)

    def test_diff_missing_checkout_404s(self):
        resp = self.client.get(
            "/api/repo/diff", params={"repo": "no-such-repo-xyz"})
        self.assertEqual(resp.status_code, 404)

    def test_diff_real_checkout_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "demo")
            os.makedirs(path)
            env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                   "GIT_COMMITTER_NAME": "t",
                   "GIT_COMMITTER_EMAIL": "t@t"}
            subprocess.run(["git", "init", "-q"], cwd=path, env=env,
                           check=True)
            old = os.environ.get("BUTTONS_REPOS_DIR")
            os.environ["BUTTONS_REPOS_DIR"] = tmp
            try:
                resp = self.client.get("/api/repo/diff",
                                       params={"repo": "demo"})
            finally:
                if old is None:
                    os.environ.pop("BUTTONS_REPOS_DIR", None)
                else:
                    os.environ["BUTTONS_REPOS_DIR"] = old
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        for key in ("repo", "branch", "clean", "status_short",
                    "diff_stat", "diff", "truncated"):
            self.assertIn(key, body)
        self.assertEqual(body["repo"], "demo")
        self.assertTrue(body["clean"])


if __name__ == "__main__":
    unittest.main()
