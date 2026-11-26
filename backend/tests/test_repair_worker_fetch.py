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

    def test_setup_worktree_passes_foreign_repo_cwd(self):
        state = {
            "issue_number": 449,
            "repo": "portfolio",
            "mode": "fresh",
            "target_files": [],
        }
        cwds = []

        def fake_run(cmd, *args, **kwargs):
            cwds.append(kwargs.get("cwd"))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch(
            "backend.repair_worker.get_checkout_path",
            return_value="/srv/repos/portfolio",
        ):
            with patch("backend.repair_worker.subprocess.run", side_effect=fake_run):
                out = repair_worker_mod.setup_worktree_node(state)
        self.assertEqual(cwds[0], "/srv/repos/portfolio")
        self.assertTrue(out["branch_name"].startswith("repair/issue-449-"))

    def test_cleanup_node_passes_foreign_repo_cwd(self):
        state = {
            "worktree_dir": "/tmp/test-wt-449",
            "repo": "portfolio",
        }
        cwds = []

        def fake_run(cmd, *args, **kwargs):
            cwds.append(kwargs.get("cwd"))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch(
            "backend.repair_worker.get_checkout_path",
            return_value="/srv/repos/portfolio",
        ):
            with patch("os.path.exists", return_value=True):
                with patch("shutil.rmtree"):
                    with patch(
                        "backend.repair_worker.subprocess.run", side_effect=fake_run
                    ):
                        repair_worker_mod.cleanup_node(state)
        self.assertEqual(cwds[0], "/srv/repos/portfolio")

    def test_edit_file_node_accumulates_tokens(self):
        state = {
            "file_contents": {"src/a.ts": "const x = 1;"},
            "issue_body": "Fix x",
            "current_errors": "",
            "input_tokens": 10,
            "output_tokens": 20,
        }
        fake_response = {
            "response": "```typescript\nconst x = 2;\n```",
            "prompt_eval_count": 100,
            "eval_count": 50,
        }
        with patch(
            "backend.repair_worker._query_ollama_raw", return_value=fake_response
        ):
            out = repair_worker_mod.edit_file_node(state)
        self.assertEqual(out["input_tokens"], 110)
        self.assertEqual(out["output_tokens"], 70)
        self.assertIn("const x = 2;", out["file_contents"]["src/a.ts"])

    def test_run_repair_records_to_ledger(self):
        recorded = []
        with (
            patch("backend.repair_worker.build_repair_graph") as mock_graph,
            patch("backend.repair_worker.preflight_ollama", return_value=None),
            patch(
                "backend.repair_worker.ledger.record",
                side_effect=lambda **kw: recorded.append(kw),
            ),
        ):
            mock_inst = mock_graph.return_value
            mock_inst.invoke.return_value = {
                "tests_passed": True,
                "input_tokens": 150,
                "output_tokens": 75,
                "pr_url": "https://github.com/arii/portfolio/pull/1",
            }
            success = repair_worker_mod.run_repair(
                issue_number=449,
                issue_body="Task body",
                repo="portfolio",
            )
        self.assertTrue(success)
        self.assertEqual(len(recorded), 1)
        entry = recorded[0]
        self.assertEqual(entry["script"], "repair_worker.py")
        self.assertEqual(entry["extra"]["repo"], "arii/portfolio")
        self.assertEqual(entry["extra"]["issue"], 449)
        self.assertEqual(entry["extra"]["status"], "APPROVED")
        self.assertEqual(
            entry["extra"]["pr_url"], "https://github.com/arii/portfolio/pull/1"
        )
        self.assertEqual(entry["usage"]["input_tokens"], 150)
        self.assertEqual(entry["usage"]["output_tokens"], 75)
        self.assertEqual(entry["usage"]["total_tokens"], 225)

    def test_ollama_timeout_respects_env_and_defaults(self):
        self.assertEqual(repair_worker_mod.ollama_timeout(), 1200)
        with patch.dict("os.environ", {"OLLAMA_TIMEOUT": "600"}):
            self.assertEqual(repair_worker_mod.ollama_timeout(), 600)
        with patch.dict("os.environ", {"OLLAMA_TIMEOUT": "invalid"}):
            self.assertEqual(repair_worker_mod.ollama_timeout(), 1200)
        self.assertEqual(repair_worker_mod.ollama_timeout(300), 300)


if __name__ == "__main__":
    unittest.main()
