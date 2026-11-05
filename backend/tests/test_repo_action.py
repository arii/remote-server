"""POST /api/repo/action: stash, discard-untracked, sync (#927)."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from backend.core import (repo_discard_untracked, repo_stash,
                          repo_sync)


def _proc(rc=0, out="", err=""):
    return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def _git_env():
    return {**os.environ, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


class FakeGit:
    """Scriptable git: status queue plus per-command procs."""

    def __init__(self, statuses=None, **procs):
        self.statuses = list(statuses or [])
        self.procs = procs
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if "status" in cmd:
            out = self.statuses.pop(0) if self.statuses else ""
            if isinstance(out, SimpleNamespace):
                return out
            return _proc(out=out)
        # Abort first: it also carries "rebase".
        for word in ("--abort", "stash", "clean", "fetch",
                     "--verify", "rebase"):
            if word in cmd:
                key = {"--abort": "abort",
                       "--verify": "verify"}.get(word, word)
                return self.procs[key]
        raise AssertionError(f"unexpected git call: {cmd}")

    def saw(self, *words):
        return any(all(w in c for w in words) for c in self.calls)


def _checkout(tmp, name="demo"):
    path = os.path.join(tmp, name)
    os.makedirs(os.path.join(path, ".git"))
    return path


class RepoStashTest(unittest.TestCase):
    def _stash(self, tmp, fake):
        _checkout(tmp)
        with mock.patch(
                "backend.core.get_repos_dir", return_value=Path(tmp)):
            return repo_stash("demo", run=fake)

    def test_clean_tree_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = self._stash(tmp, FakeGit(statuses=[""]))
        self.assertTrue(body["ok"])
        self.assertTrue(body["clean"])
        self.assertIn("nothing to stash", body["detail"])

    def test_dirty_tree_stashes_and_reports_clean(self):
        fake = FakeGit(statuses=[" M a.py\n", ""],
                       stash=_proc(out="Saved\n"))
        with tempfile.TemporaryDirectory() as tmp:
            body = self._stash(tmp, fake)
        self.assertTrue(body["ok"])
        self.assertTrue(body["clean"])
        self.assertEqual(body["detail"], "changes stashed")
        self.assertTrue(fake.saw("stash", "push", "-u"))

    def test_stash_failure_names_manual_recovery(self):
        fake = FakeGit(statuses=[" M a.py\n"],
                       stash=_proc(rc=1, err="fatal: boom"))
        with tempfile.TemporaryDirectory() as tmp:
            body = self._stash(tmp, fake)
        self.assertFalse(body["ok"])
        self.assertIn("by hand", body["detail"])
        self.assertNotIn("boom", body["detail"])

    def test_bad_name_raises_value_error(self):
        with self.assertRaises(ValueError):
            repo_stash("a/b/c", run=FakeGit())

    def test_missing_checkout_raises_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                    "backend.core.get_repos_dir", return_value=Path(tmp)):
                with self.assertRaises(FileNotFoundError):
                    repo_stash("nope", run=FakeGit())

    def test_real_checkout_stash_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "demo")
            os.makedirs(path)
            env = _git_env()
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
            with open(os.path.join(path, "new.txt"), "w") as fh:
                fh.write("untracked\n")
            with mock.patch(
                    "backend.core.get_repos_dir", return_value=Path(tmp)):
                body = repo_stash("demo")
            self.assertTrue(body["ok"])
            self.assertTrue(body["clean"])
            listed = subprocess.run(
                ["git", "stash", "list"], cwd=path, env=env,
                stdout=subprocess.PIPE, text=True, check=True)
            self.assertIn("console diff-modal", listed.stdout)


class RepoDiscardTest(unittest.TestCase):
    def _discard(self, tmp, fake):
        _checkout(tmp)
        with mock.patch(
                "backend.core.get_repos_dir", return_value=Path(tmp)):
            return repo_discard_untracked("demo", run=fake)

    def test_no_untracked_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = self._discard(tmp, FakeGit(statuses=[" M a.py\n"]))
        self.assertTrue(body["ok"])
        self.assertFalse(body["clean"])
        self.assertIn("no untracked", body["detail"])

    def test_untracked_removed_and_counted(self):
        fake = FakeGit(statuses=["?? new.txt\n", ""],
                       clean=_proc(out="Removing new.txt\n"))
        with tempfile.TemporaryDirectory() as tmp:
            body = self._discard(tmp, fake)
        self.assertTrue(body["ok"])
        self.assertTrue(body["clean"])
        self.assertIn("1 untracked", body["detail"])
        argv = [c for c in fake.calls if "clean" in c][0]
        self.assertNotIn("-x", argv)

    def test_clean_failure_names_manual_recovery(self):
        fake = FakeGit(statuses=["?? new.txt\n"],
                       clean=_proc(rc=1, err="fatal: boom"))
        with tempfile.TemporaryDirectory() as tmp:
            body = self._discard(tmp, fake)
        self.assertFalse(body["ok"])
        self.assertIn("by hand", body["detail"])
        self.assertNotIn("boom", body["detail"])

    def test_real_checkout_discards_untracked_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "demo")
            os.makedirs(path)
            env = _git_env()
            subprocess.run(["git", "init", "-q"], cwd=path, env=env,
                           check=True)
            with open(os.path.join(path, "keep.py"), "w") as fh:
                fh.write("x = 1\n")
            subprocess.run(["git", "add", "."], cwd=path, env=env,
                           check=True)
            subprocess.run(["git", "commit", "-qm", "one"], cwd=path,
                           env=env, check=True)
            with open(os.path.join(path, "keep.py"), "a") as fh:
                fh.write("y = 2\n")
            drop = os.path.join(path, "drop.txt")
            with open(drop, "w") as fh:
                fh.write("junk\n")
            with mock.patch(
                    "backend.core.get_repos_dir", return_value=Path(tmp)):
                body = repo_discard_untracked("demo")
            self.assertTrue(body["ok"])
            self.assertFalse(os.path.exists(drop))
            with open(os.path.join(path, "keep.py")) as fh:
                self.assertIn("y = 2", fh.read())


class RepoSyncTest(unittest.TestCase):
    def _sync(self, tmp, fake):
        _checkout(tmp)
        with mock.patch(
                "backend.core.get_repos_dir", return_value=Path(tmp)):
            return repo_sync("demo", run=fake)

    def _clean_fake(self, **procs):
        return FakeGit(statuses=["", ""], **procs)

    def test_dirty_tree_refuses_with_plain_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = self._sync(tmp, FakeGit(statuses=[" M a.py\n"]))
        self.assertFalse(body["ok"])
        self.assertIn("stash or commit", body["detail"])

    def test_fetch_failure_reports_network(self):
        fake = self._clean_fake(fetch=_proc(rc=128, err="fatal: net"))
        with tempfile.TemporaryDirectory() as tmp:
            body = self._sync(tmp, fake)
        self.assertFalse(body["ok"])
        self.assertIn("check network", body["detail"])
        self.assertNotIn("fatal", body["detail"])

    def test_missing_origin_main_reports_sync_by_hand(self):
        fake = self._clean_fake(fetch=_proc(out=""),
                                verify=_proc(rc=1, out=""))
        with tempfile.TemporaryDirectory() as tmp:
            body = self._sync(tmp, fake)
        self.assertFalse(body["ok"])
        self.assertIn("origin/main not found", body["detail"])

    def test_rebase_success_reports_rebased(self):
        fake = self._clean_fake(
            fetch=_proc(out=""), verify=_proc(out="abc\n"),
            rebase=_proc(out="Successfully rebased\n"))
        with tempfile.TemporaryDirectory() as tmp:
            body = self._sync(tmp, fake)
        self.assertTrue(body["ok"])
        self.assertEqual(body["detail"], "rebased onto origin/main")

    def test_up_to_date_reports_plainly(self):
        fake = self._clean_fake(
            fetch=_proc(out=""), verify=_proc(out="abc\n"),
            rebase=_proc(out="Current branch main is up to date.\n"))
        with tempfile.TemporaryDirectory() as tmp:
            body = self._sync(tmp, fake)
        self.assertTrue(body["ok"])
        self.assertIn("up to date", body["detail"])

    def test_conflict_aborts_and_leaves_tree(self):
        fake = self._clean_fake(
            fetch=_proc(out=""), verify=_proc(out="abc\n"),
            rebase=_proc(rc=1, err="CONFLICT (content)"),
            abort=_proc(out=""))
        with tempfile.TemporaryDirectory() as tmp:
            body = self._sync(tmp, fake)
        self.assertFalse(body["ok"])
        self.assertIn("aborted, tree unchanged", body["detail"])
        self.assertTrue(fake.saw("rebase", "--abort"))

    def test_real_checkout_without_origin_reports_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "demo")
            os.makedirs(path)
            env = _git_env()
            subprocess.run(["git", "init", "-q"], cwd=path, env=env,
                           check=True)
            subprocess.run(["git", "commit", "-qm", "one",
                            "--allow-empty"], cwd=path, env=env,
                           check=True)
            with mock.patch(
                    "backend.core.get_repos_dir", return_value=Path(tmp)):
                body = repo_sync("demo")
            self.assertFalse(body["ok"])
            self.assertIn("check network", body["detail"])


class RepoActionEndpointTest(unittest.TestCase):
    def setUp(self):
        from backend.server import app

        self.client = TestClient(app)

    def _post(self, **kw):
        body = {"repo": "demo", "action": "stash",
                "intent": "repo-action", "confirmed": True}
        body.update(kw)
        return self.client.post("/api/repo/action", json=body)

    def test_unknown_action_400s(self):
        self.assertEqual(self._post(action="nuke").status_code, 400)

    def test_missing_confirmation_400s(self):
        self.assertEqual(self._post(confirmed=False).status_code, 400)
        self.assertEqual(
            self._post(intent="wrong").status_code, 400)

    def test_bad_repo_name_400s(self):
        self.assertEqual(
            self._post(repo="a/b/c").status_code, 400)

    def test_missing_checkout_404s(self):
        self.assertEqual(
            self._post(repo="no-such-repo-xyz").status_code, 404)

    def test_stash_noop_shape_on_real_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "demo")
            os.makedirs(path)
            env = _git_env()
            subprocess.run(["git", "init", "-q"], cwd=path, env=env,
                           check=True)
            subprocess.run(["git", "commit", "-qm", "one",
                            "--allow-empty"], cwd=path, env=env,
                           check=True)
            old = os.environ.get("BUTTONS_REPOS_DIR")
            os.environ["BUTTONS_REPOS_DIR"] = tmp
            try:
                resp = self._post()
            finally:
                if old is None:
                    os.environ.pop("BUTTONS_REPOS_DIR", None)
                else:
                    os.environ["BUTTONS_REPOS_DIR"] = old
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        for key in ("ok", "repo", "action", "clean", "detail"):
            self.assertIn(key, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["action"], "stash")


if __name__ == "__main__":
    unittest.main()
