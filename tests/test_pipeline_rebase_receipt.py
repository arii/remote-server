import os
import tempfile
import unittest
from unittest import mock

import pipeline


class TestPrepareWorktreeForSubmit(unittest.TestCase):
    def test_mirrors_receipt_when_present(self):
        with (
            tempfile.TemporaryDirectory() as repo_dir,
            tempfile.TemporaryDirectory() as wt_dir,
        ):
            receipt_file = os.path.join(repo_dir, ".agent_tool_receipt.json")
            with open(receipt_file, "w") as f:
                f.write('{"usedRag": true, "tools": ["td rag"]}')

            logs = []
            with mock.patch("subprocess.run") as mock_sub:
                mock_sub.return_value.returncode = 0
                res = pipeline.prepare_worktree_for_submit(
                    repo_dir, wt_dir, log_fn=logs.append
                )

            self.assertTrue(res)
            dest_receipt = os.path.join(wt_dir, ".agent_tool_receipt.json")
            self.assertTrue(os.path.isfile(dest_receipt))
            with open(dest_receipt) as f:
                self.assertIn('"usedRag": true', f.read())
            self.assertIn("mirrored .agent_tool_receipt.json to worktree root", logs)

    def test_handles_missing_receipt_gracefully(self):
        with (
            tempfile.TemporaryDirectory() as repo_dir,
            tempfile.TemporaryDirectory() as wt_dir,
        ):
            logs = []
            with mock.patch("subprocess.run") as mock_sub:
                mock_sub.return_value.returncode = 0
                res = pipeline.prepare_worktree_for_submit(
                    repo_dir, wt_dir, log_fn=logs.append
                )

            self.assertTrue(res)
            dest_receipt = os.path.join(wt_dir, ".agent_tool_receipt.json")
            self.assertFalse(os.path.exists(dest_receipt))

    def test_rebase_conflict_aborts_cleanly(self):
        with (
            tempfile.TemporaryDirectory() as repo_dir,
            tempfile.TemporaryDirectory() as wt_dir,
        ):
            logs = []

            def fake_run(cmd, **kwargs):
                ret = mock.MagicMock()
                if "rebase" in cmd and "--abort" not in cmd:
                    ret.returncode = 1
                    ret.stderr = "CONFLICT (content): Merge conflict in file.py"
                else:
                    ret.returncode = 0
                    ret.stderr = ""
                return ret

            with mock.patch("subprocess.run", side_effect=fake_run) as mock_sub:
                res = pipeline.prepare_worktree_for_submit(
                    repo_dir, wt_dir, log_fn=logs.append
                )

            self.assertTrue(res)
            # Confirms git rebase --abort was called
            calls = [c[0][0] for c in mock_sub.call_args_list]
            self.assertTrue(
                any("rebase" in cmd and "--abort" in cmd for cmd in calls),
                f"rebase --abort not called in {calls}",
            )
            self.assertTrue(
                any(
                    "warning: auto-rebase onto origin/main failed" in log
                    for log in logs
                ),
                f"rebase warning not logged in {logs}",
            )

    def test_stage_submit_calls_prepare_worktree(self):
        with (
            tempfile.TemporaryDirectory() as repo_dir,
            tempfile.TemporaryDirectory() as wt_dir,
        ):
            pipe = pipeline.Pipeline(repo_dir, "test intent", issue_id=1394)
            with (
                mock.patch("pipeline.prepare_worktree_for_submit") as mock_prep,
                mock.patch.object(
                    pipe,
                    "td_run",
                    return_value=(0, '{"url": "https://github.com/pr/1"}'),
                ),
            ):
                ok, url = pipe.stage_submit(wt_dir, "test title", "test body")

            self.assertTrue(ok)
            self.assertEqual(url, "https://github.com/pr/1")
            mock_prep.assert_called_once()
            self.assertEqual(mock_prep.call_args[0][0], repo_dir)
            self.assertEqual(mock_prep.call_args[0][1], wt_dir)
