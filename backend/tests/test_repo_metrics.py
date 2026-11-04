"""GET /api/repo/metrics: worklist counts for the prefetch strip (#897)."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from backend.core import get_repo_metrics


def _proc(rc=0, out="", err=""):
    return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def _fake_run_factory(branch="main", short="", stat="", todo_out="",
                      todo_rc=1):
    def fake_run(cmd, **kwargs):
        if "rev-parse" in cmd:
            return _proc(out=branch + "\n")
        if "grep" in cmd:
            return _proc(rc=todo_rc, out=todo_out)
        if "status" in cmd:
            return _proc(out=short)
        if "diff" in cmd and "--stat" in cmd:
            return _proc(out=stat)
        raise AssertionError(f"unexpected call: {cmd}")

    return fake_run


def _git_env():
    return {**os.environ, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


class RepoMetricsCoreTest(unittest.TestCase):
    def _metrics(self, tmp, **kwargs):
        path = os.path.join(tmp, "demo")
        os.makedirs(os.path.join(path, ".git"))
        with mock.patch(
                "backend.core.get_repos_dir", return_value=Path(tmp)):
            return get_repo_metrics("demo",
                                    run=_fake_run_factory(**kwargs))

    def test_clean_repo_zeroes(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = self._metrics(tmp)
        self.assertEqual(body["repo"], "demo")
        self.assertEqual(body["branch"], "main")
        self.assertTrue(body["clean"])
        self.assertEqual(body["status_count"], 0)
        self.assertEqual(body["todo_count"], 0)
        self.assertEqual(body["todo_hits"], [])
        self.assertEqual(body["diff_files"], 0)

    def test_dirty_repo_counts_and_hits(self):
        short = " M a.py\n?? b.py\n"
        stat = (" a.py | 2 +-\n b.py | 5 +++--\n"
                " 2 files changed, 4 insertions(+), 3 deletions(-)\n")
        todos = ("a.py:10:# TODO: fix me\n"
                 "a.py:20:# FIXME: and this\n")
        with tempfile.TemporaryDirectory() as tmp:
            body = self._metrics(tmp, short=short, stat=stat,
                                 todo_out=todos, todo_rc=0)
        self.assertFalse(body["clean"])
        self.assertEqual(body["status_count"], 2)
        self.assertEqual(body["todo_count"], 2)
        self.assertEqual(body["diff_files"], 2)
        self.assertEqual(body["todo_hits"][0],
                         {"file": "a.py", "line": 10,
                          "text": "# TODO: fix me"})

    def test_bad_name_raises_value_error(self):
        with self.assertRaises(ValueError):
            get_repo_metrics("a/b/c", run=_fake_run_factory())

    def test_missing_checkout_raises_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                    "backend.core.get_repos_dir",
                    return_value=Path(tmp)):
                with self.assertRaises(FileNotFoundError):
                    get_repo_metrics("nope", run=_fake_run_factory())

    def test_git_failure_raises_runtime_error(self):
        def failing(cmd, **kwargs):
            if "rev-parse" in cmd:
                return _proc(out="main\n")
            return _proc(rc=128, err="fatal: boom")

        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "demo", ".git"))
            with mock.patch(
                    "backend.core.get_repos_dir",
                    return_value=Path(tmp)):
                with self.assertRaises(RuntimeError):
                    get_repo_metrics("demo", run=failing)

    def test_grep_failure_raises_runtime_error(self):
        def failing(cmd, **kwargs):
            if "grep" in cmd:
                return _proc(rc=2, err="fatal: not a git repo")
            return _fake_run_factory()(cmd, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "demo", ".git"))
            with mock.patch(
                    "backend.core.get_repos_dir",
                    return_value=Path(tmp)):
                with self.assertRaises(RuntimeError):
                    get_repo_metrics("demo", run=failing)


class RepoMetricsRealGitTest(unittest.TestCase):
    def test_real_checkout_reports_real_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "demo")
            os.makedirs(path)
            env = _git_env()
            subprocess.run(["git", "init", "-q"], cwd=path, env=env,
                           check=True)
            with open(os.path.join(path, "a.py"), "w") as fh:
                fh.write("x = 1  # TODO: real one\n")
            subprocess.run(["git", "add", "."], cwd=path, env=env,
                           check=True)
            subprocess.run(["git", "commit", "-qm", "one"], cwd=path,
                           env=env, check=True)
            with open(os.path.join(path, "a.py"), "a") as fh:
                fh.write("y = 2\n")
            with mock.patch(
                    "backend.core.get_repos_dir",
                    return_value=Path(tmp)):
                body = get_repo_metrics("demo")
        self.assertFalse(body["clean"])
        self.assertEqual(body["status_count"], 1)
        self.assertEqual(body["todo_count"], 1)
        self.assertEqual(body["todo_hits"][0]["file"], "a.py")
        self.assertEqual(body["diff_files"], 1)


class RepoMetricsEndpointTest(unittest.TestCase):
    def setUp(self):
        from backend.server import app

        self.client = TestClient(app)

    def test_metrics_validates_repo_name(self):
        resp = self.client.get("/api/repo/metrics",
                               params={"repo": "a/b/c"})
        self.assertEqual(resp.status_code, 400)

    def test_metrics_missing_checkout_404s(self):
        resp = self.client.get(
            "/api/repo/metrics", params={"repo": "no-such-repo-xyz"})
        self.assertEqual(resp.status_code, 404)

    def test_metrics_real_checkout_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "demo")
            os.makedirs(path)
            subprocess.run(["git", "init", "-q"], cwd=path, env=_git_env(),
                           check=True)
            old = os.environ.get("BUTTONS_REPOS_DIR")
            os.environ["BUTTONS_REPOS_DIR"] = tmp
            try:
                resp = self.client.get("/api/repo/metrics",
                                       params={"repo": "demo"})
            finally:
                if old is None:
                    os.environ.pop("BUTTONS_REPOS_DIR", None)
                else:
                    os.environ["BUTTONS_REPOS_DIR"] = old
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        for key in ("repo", "branch", "clean", "status_count",
                    "todo_count", "todo_hits", "diff_stat",
                    "diff_files"):
            self.assertIn(key, body)
        self.assertEqual(body["repo"], "demo")


if __name__ == "__main__":
    unittest.main()
