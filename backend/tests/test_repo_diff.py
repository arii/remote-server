"""GET /api/repo/diff: status + diff stat + truncated full diff (#663, #927)."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from backend.core.git_helpers import (DIFF_CAP, DIFF_FILES_SHOWN,
                                         STATUS_SHORT_LINES_CAP, get_repo_diff)


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
                "backend.core.repos.get_repos_dir", return_value=Path(tmp)):
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
                    "backend.core.repos.get_repos_dir", return_value=Path(tmp)):
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
                    "backend.core.repos.get_repos_dir", return_value=Path(tmp)):
                with self.assertRaises(RuntimeError):
                    get_repo_diff("demo", run=failing)


def _head_aware_fake(branch="main", short="", stat="", diff="",
                     numstat="", head_born=True):
    """Fake git honoring the diff HEAD argv (#927)."""
    calls = []

    def fake(cmd, **kwargs):
        calls.append(cmd)
        if cmd[-2:] == ["--verify", "HEAD"] or \
                ("--verify" in cmd and "HEAD" in cmd):
            return _proc(rc=0 if head_born else 1, out="")
        if "rev-parse" in cmd:
            return _proc(out=branch + "\n")
        if "status" in cmd:
            return _proc(out=short)
        if "diff" in cmd and "--stat" in cmd:
            return _proc(out=stat)
        if "diff" in cmd and "--numstat" in cmd:
            return _proc(out=numstat)
        if "diff" in cmd:
            if "HEAD" in cmd and not head_born:
                return _proc(rc=128, err="fatal: bad revision 'HEAD'")
            return _proc(out=diff)
        raise AssertionError(f"unexpected git call: {cmd}")

    fake.calls = calls
    return fake


class RepoDiffFilesTest(unittest.TestCase):
    def _diff(self, tmp, **kwargs):
        path = os.path.join(tmp, "demo")
        os.makedirs(os.path.join(path, ".git"))
        fake = _head_aware_fake(**kwargs)
        with mock.patch(
                "backend.core.repos.get_repos_dir", return_value=Path(tmp)):
            body = get_repo_diff("demo", run=fake)
        return body, fake

    def test_diff_goes_against_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, fake = self._diff(tmp, short=" M a.py\n")
        diff_calls = [c for c in fake.calls if "diff" in c]
        self.assertTrue(diff_calls)
        for call in diff_calls:
            self.assertIn("HEAD", call)

    def test_files_carry_status_and_numstat_counts(self):
        short = "M  staged.py\n M work.py\n?? new.txt\n"
        numstat = "3\t1\tstaged.py\n10\t0\twork.py\n"
        with tempfile.TemporaryDirectory() as tmp:
            body, _ = self._diff(tmp, short=short, numstat=numstat)
        self.assertFalse(body["files_truncated"])
        self.assertFalse(body["status_truncated"])
        by_path = {f["path"]: f for f in body["files"]}
        self.assertEqual(by_path["staged.py"]["status"], "M")
        self.assertTrue(by_path["staged.py"]["staged"])
        self.assertFalse(by_path["staged.py"]["unstaged"])
        self.assertEqual(
            (by_path["staged.py"]["additions"],
             by_path["staged.py"]["deletions"]), (3, 1))
        self.assertEqual(by_path["work.py"]["status"], "M")
        self.assertFalse(by_path["work.py"]["staged"])
        self.assertTrue(by_path["work.py"]["unstaged"])
        self.assertEqual(by_path["new.txt"]["status"], "??")
        self.assertEqual(
            (by_path["new.txt"]["additions"],
             by_path["new.txt"]["deletions"]), (0, 0))

    def test_files_truncate_with_flag(self):
        short = "".join(f" M f{i:04d}.py\n" for i in range(250))
        with tempfile.TemporaryDirectory() as tmp:
            body, _ = self._diff(tmp, short=short)
        self.assertEqual(len(body["files"]), DIFF_FILES_SHOWN)
        self.assertTrue(body["files_truncated"])
        self.assertFalse(body["clean"])

    def test_status_truncates_but_clean_stays_honest(self):
        short = "".join(f" M f{i:04d}.py\n"
                        for i in range(STATUS_SHORT_LINES_CAP + 50))
        with tempfile.TemporaryDirectory() as tmp:
            body, _ = self._diff(tmp, short=short)
        self.assertTrue(body["status_truncated"])
        self.assertEqual(len(body["status_short"].splitlines()),
                         STATUS_SHORT_LINES_CAP)
        self.assertFalse(body["clean"])

    def test_unborn_head_falls_back_to_plain_diff(self):
        short = "A  first.py\n"
        with tempfile.TemporaryDirectory() as tmp:
            body, fake = self._diff(
                tmp, short=short, stat=" first.py | 1 +\n",
                diff="diff --git a/first.py b/first.py\n",
                head_born=False)
        self.assertEqual(body["diff_stat"], " first.py | 1 +\n")
        self.assertIn("first.py", body["diff"])
        plain = [c for c in fake.calls
                 if "diff" in c and "HEAD" not in c]
        self.assertTrue(plain)


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
                    "backend.core.repos.get_repos_dir", return_value=Path(tmp)):
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
                    "status_truncated", "diff_stat", "diff",
                    "truncated", "files", "files_truncated"):
            self.assertIn(key, body)
        self.assertEqual(body["repo"], "demo")
        self.assertTrue(body["clean"])


class RepoDiffUnbornHeadTest(unittest.TestCase):
    def test_fresh_repo_without_commits_stays_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "demo")
            os.makedirs(path)
            env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                   "GIT_COMMITTER_NAME": "t",
                   "GIT_COMMITTER_EMAIL": "t@t"}
            subprocess.run(["git", "init", "-q"], cwd=path, env=env,
                           check=True)
            with open(os.path.join(path, "first.py"), "w") as fh:
                fh.write("x = 1\n")
            subprocess.run(["git", "add", "."], cwd=path, env=env,
                           check=True)
            with mock.patch(
                    "backend.core.repos.get_repos_dir", return_value=Path(tmp)):
                body = get_repo_diff("demo")
        self.assertFalse(body["clean"])
        self.assertIn("first.py", body["status_short"])
        self.assertEqual(len(body["files"]), 1)
        self.assertEqual(body["files"][0]["path"], "first.py")


if __name__ == "__main__":
    unittest.main()
