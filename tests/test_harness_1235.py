"""Regression tests for issue #1235: worktree node_modules link +
investigation-brief plan trigger.

Run: python3 -m pytest tests/test_harness_1235.py
"""

import os
import tempfile
import unittest

import coder
import s5tools


class TestLinkWorktreeNodeModules(unittest.TestCase):
    def test_links_store_into_fresh_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = os.path.join(tmp, "store")
            os.makedirs(os.path.join(store, "node_modules"))
            root = os.path.join(tmp, "root")
            os.makedirs(os.path.join(root, "frontend"))
            self.assertTrue(s5tools.link_worktree_node_modules(root, store))
            link = os.path.join(root, "frontend", "node_modules")
            self.assertTrue(os.path.islink(link))
            self.assertTrue(os.path.isdir(os.path.realpath(link)))

    def test_missing_store_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "root")
            os.makedirs(os.path.join(root, "frontend"))
            self.assertFalse(
                s5tools.link_worktree_node_modules(root, os.path.join(tmp, "nope"))
            )

    def test_existing_target_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = os.path.join(tmp, "store")
            os.makedirs(os.path.join(store, "node_modules"))
            root = os.path.join(tmp, "root")
            target = os.path.join(root, "frontend", "node_modules")
            os.makedirs(target)
            self.assertFalse(s5tools.link_worktree_node_modules(root, store))
            self.assertTrue(os.path.isdir(target))
            self.assertFalse(os.path.islink(target))

    def test_bad_paths_never_raise(self):
        self.assertFalse(s5tools.link_worktree_node_modules("", ""))
        self.assertFalse(
            s5tools.link_worktree_node_modules("/nonexistent/x", "/nonexistent/y")
        )


class TestNeedsPlanInvestigation(unittest.TestCase):
    def setUp(self):
        os.environ.pop(coder.PLAN_ENV, None)

    def tearDown(self):
        os.environ.pop(coder.PLAN_ENV, None)

    def test_investigation_verb_no_files(self):
        self.assertTrue(coder.needs_plan("find where the turn waits"))

    def test_grep_verb(self):
        self.assertTrue(coder.needs_plan("grep the codebase for X"))

    def test_two_files_still_plans(self):
        self.assertTrue(coder.needs_plan("edit a.py and b.py"))

    def test_single_file_no_verb_unchanged(self):
        self.assertFalse(coder.needs_plan("fix frontend/src/views/NowView.tsx"))

    def test_never_override_wins(self):
        os.environ[coder.PLAN_ENV] = "never"
        self.assertFalse(coder.needs_plan("find where the turn waits"))


if __name__ == "__main__":
    unittest.main()
