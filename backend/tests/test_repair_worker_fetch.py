"""repair_worker fetch-corruption attribution (audit fallback batch).

parse_issue_node must raise a RuntimeError naming fetch corruption —
never a spec-parse/unscaffolded error — when `gh issue view` exits 0
with non-JSON stdout. gh is stubbed at backend.repair_worker.subprocess
so the test never touches the network.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend import repair_worker as repair_worker_mod


class FetchCorruptionTest(unittest.TestCase):
    def test_non_json_stdout_raises_fetch_corruption(self):
        proc = SimpleNamespace(returncode=0, stdout="this is not json{{{", stderr="")
        with patch("backend.repair_worker.subprocess") as subprocess_mock:
            subprocess_mock.run.return_value = proc
            with self.assertRaises(RuntimeError) as ctx:
                repair_worker_mod.parse_issue_node(
                    {"issue_number": 9, "issue_body": "", "repo": "remote-server"}
                )
        subprocess_mock.run.assert_called_once()
        exc = ctx.exception
        self.assertIn("fetch corruption", str(exc))
        self.assertIn("not a spec error", str(exc))
        self.assertNotIsInstance(exc, repair_worker_mod.UnscaffoldedIssueError)


class SetupWorktreeNodeTest(unittest.TestCase):
    def test_resume_mode_existing_branch(self):
        state = {
            "issue_number": 42,
            "mode": "resume",
            "branch_name": "repair/issue-42-existing",
            "target_files": [],
        }
        commands = []

        def fake_run(cmd, *args, **kwargs):
            commands.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch("backend.repair_worker.subprocess.run", side_effect=fake_run):
            out = repair_worker_mod.setup_worktree_node(state)
        self.assertEqual(out["branch_name"], "repair/issue-42-existing")
        self.assertEqual(
            commands[0],
            ["git", "rev-parse", "--verify", "repair/issue-42-existing"],
        )
        self.assertEqual(
            commands[1],
            ["git", "worktree", "add", out["worktree_dir"], "repair/issue-42-existing"],
        )

    def test_resume_mode_deleted_branch_falls_back_to_fresh(self):
        state = {
            "issue_number": 42,
            "mode": "resume",
            "branch_name": "repair/issue-42-deleted",
            "target_files": [],
        }
        commands = []

        def fake_run(cmd, *args, **kwargs):
            commands.append(cmd)
            if cmd[:3] == ["git", "rev-parse", "--verify"]:
                return SimpleNamespace(returncode=1, stdout="", stderr="not found")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch("backend.repair_worker.subprocess.run", side_effect=fake_run):
            out = repair_worker_mod.setup_worktree_node(state)
        self.assertTrue(out["branch_name"].startswith("repair/issue-42-"))
        self.assertNotEqual(out["branch_name"], "repair/issue-42-deleted")
        self.assertEqual(commands[1][:4], ["git", "worktree", "add", "-b"])

    def test_fresh_mode_mints_branch(self):
        state = {
            "issue_number": 42,
            "mode": "fresh",
            "target_files": [],
        }
        commands = []

        def fake_run(cmd, *args, **kwargs):
            commands.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch("backend.repair_worker.subprocess.run", side_effect=fake_run):
            out = repair_worker_mod.setup_worktree_node(state)
        self.assertTrue(out["branch_name"].startswith("repair/issue-42-"))
        self.assertEqual(commands[0][:4], ["git", "worktree", "add", "-b"])


if __name__ == "__main__":
    unittest.main()
