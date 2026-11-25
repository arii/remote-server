import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "repo-closeout.py"
spec = importlib.util.spec_from_file_location("closeout", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CloseoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.origin = self.root / "origin.git"
        self.repo = self.root / "repo"
        self.run_git(self.root, "init", "--bare", str(self.origin))
        self.run_git(self.root, "clone", str(self.origin), str(self.repo))
        self.g("config", "user.email", "test@example.invalid")
        self.g("config", "user.name", "Test")
        self.g("checkout", "-b", "main")
        (self.repo / "file").write_text("base")
        self.g("add", "file")
        self.g("commit", "-m", "base")
        self.g("push", "-u", "origin", "main")

    @staticmethod
    def run_git(cwd, *args):
        return subprocess.check_output(
            ["git", "-C", str(cwd), *args], stderr=subprocess.DEVNULL, text=True
        ).strip()

    def g(self, *args):
        return self.run_git(self.repo, *args)

    def tree(self, branch="agent/issue-1"):
        path = self.root / "tree"
        self.g("worktree", "add", "-b", branch, str(path), "main")
        return path

    def test_clean_landed_tree_removed(self):
        path = self.tree()
        result = module.closeout(self.repo, True)
        self.assertFalse(path.exists())
        self.assertFalse(result["needs_owner"])

    def test_untracked_agent_work_preserved(self):
        path = self.tree()
        (path / "unknown").write_text("irreplaceable")
        module.closeout(self.repo, True)
        self.assertEqual((path / "unknown").read_text(), "irreplaceable")

    def test_ignored_work_preserved(self):
        path = self.tree()
        self.g("config", "core.excludesFile", str(self.root / "ignore"))
        (self.root / "ignore").write_text("unknown\n")
        (path / "unknown").write_text("ignored but valuable")
        module.closeout(self.repo, True)
        self.assertTrue(path.exists())

    def test_dirty_primary_never_committed_or_reset(self):
        before = self.g("rev-parse", "HEAD")
        (self.repo / "file").write_text("unfinished")
        result = module.closeout(self.repo, True)
        self.assertTrue(result["needs_owner"])
        self.assertEqual(self.g("rev-parse", "HEAD"), before)
        self.assertEqual((self.repo / "file").read_text(), "unfinished")

    def test_report_only_keeps_tree(self):
        path = self.tree()
        module.closeout(self.repo)
        self.assertTrue(path.exists())

    def test_unmerged_and_locked_trees_kept(self):
        path = self.tree()
        self.run_git(path, "config", "user.email", "test@example.invalid")
        self.run_git(path, "config", "user.name", "Test")
        (path / "file").write_text("not merged")
        self.run_git(path, "commit", "-am", "unmerged")
        module.closeout(self.repo, True)
        self.assertTrue(path.exists())
        self.g("worktree", "lock", str(path))
        module.closeout(self.repo, True)
        self.assertTrue(path.exists())

    def test_empty_operator_tree_kept(self):
        path = self.tree("feat/in-progress")
        module.closeout(self.repo, True)
        self.assertTrue(path.exists())

    def test_merged_pr_must_match_actual_head(self):
        path = self.tree()
        (path / "file").write_text("squashed work")
        self.run_git(path, "commit", "-am", "work")
        tip = self.run_git(path, "rev-parse", "HEAD")
        bindir = self.root / "bin"
        bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text("#!/bin/sh\necho stale-head\n")
        gh.chmod(0o755)
        with patch.dict(
            os.environ, {"PATH": str(bindir) + os.pathsep + os.environ["PATH"]}
        ):
            module.closeout(self.repo, True)
            self.assertTrue(path.exists())
            gh.write_text("#!/bin/sh\necho " + tip + "\n")
            module.closeout(self.repo, True)
            self.assertFalse(path.exists())

    def test_failed_fetch_never_prunes(self):
        path = self.tree()
        self.g("remote", "set-url", "origin", str(self.root / "missing"))
        result = module.closeout(self.repo, True)
        self.assertTrue(result["needs_owner"])
        self.assertTrue(path.exists())

    def test_main_fast_forward_does_not_run_deploy_hook(self):
        old = self.g("rev-parse", "HEAD")
        (self.repo / "file").write_text("landed")
        self.g("commit", "-am", "landed")
        new = self.g("rev-parse", "HEAD")
        self.g("push", "origin", "main")
        self.g("reset", "--hard", old)
        hook = self.repo / ".git/hooks/post-merge"
        hook.write_text("#!/bin/sh\ntouch hook-ran\n")
        hook.chmod(0o755)
        module.closeout(self.repo, True)
        self.assertEqual(self.g("rev-parse", "HEAD"), new)
        self.assertFalse((self.repo / "hook-ran").exists())
