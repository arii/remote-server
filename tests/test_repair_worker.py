#!/usr/bin/env python3
"""Unit tests for the autonomous repair worker LangGraph state machine."""

import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

import scripts.repair_worker as rw


class TestRepairWorkerStateGraph(unittest.TestCase):
    def setUp(self):
        self.sample_task_spec = """---
name: Deterministic Task
---

## Target Files
- `src/calculator.py`

## Verification Command
```bash
python3 -m unittest discover -s tests -p 'test_calc.py'
```

## Required Changes
1. Fix addition overflow bug
"""

    @patch("subprocess.run")
    def test_parse_issue_node_from_gh(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = '{"body": "## Target Files\\n- app.py\\n\\n## Verification Command\\n`pytest`"}'
        mock_run.return_value = mock_proc

        state: rw.AgentState = {
            "issue_number": 42,
            "issue_body": "",
            "target_files": [],
            "verification_cmd": "",
            "file_contents": {},
            "current_errors": "",
            "retry_count": 0,
            "ast_valid": False,
            "tests_passed": False,
            "worktree_dir": "",
            "branch_name": "",
        }

        updates = rw.parse_issue_node(state)
        self.assertEqual(updates["target_files"], ["app.py"])
        self.assertEqual(updates["verification_cmd"], "pytest")
        self.assertIn("Target Files", updates["issue_body"])
        mock_run.assert_called_once_with(
            ["gh", "issue", "view", "42", "--json", "body"],
            stdout=rw.subprocess.PIPE,
            stderr=rw.subprocess.PIPE,
            text=True,
        )

    def test_parse_issue_node_with_existing_body(self):
        state: rw.AgentState = {
            "issue_number": 42,
            "issue_body": self.sample_task_spec,
            "target_files": [],
            "verification_cmd": "",
            "file_contents": {},
            "current_errors": "",
            "retry_count": 0,
            "ast_valid": False,
            "tests_passed": False,
            "worktree_dir": "",
            "branch_name": "",
        }

        updates = rw.parse_issue_node(state)
        self.assertEqual(updates["target_files"], ["src/calculator.py"])
        self.assertEqual(updates["verification_cmd"], "python3 -m unittest discover -s tests -p 'test_calc.py'")

    @patch("subprocess.run")
    def test_setup_worktree_node(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_run.return_value = mock_proc

        temp_dir = tempfile.mkdtemp()
        try:
            file_path = os.path.join(temp_dir, "test_file.py")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("def foo(): return 1\n")

            state: rw.AgentState = {
                "issue_number": 12,
                "issue_body": "",
                "target_files": ["test_file.py"],
                "verification_cmd": "",
                "file_contents": {},
                "current_errors": "",
                "retry_count": 0,
                "ast_valid": False,
                "tests_passed": False,
                "worktree_dir": temp_dir,
                "branch_name": "repair/issue-12-test",
            }

            updates = rw.setup_worktree_node(state)
            self.assertEqual(updates["worktree_dir"], temp_dir)
            self.assertEqual(updates["branch_name"], "repair/issue-12-test")
            self.assertEqual(updates["file_contents"], {"test_file.py": "def foo(): return 1\n"})
            mock_run.assert_called_once()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    @patch("scripts.repair_worker._query_ollama")
    def test_edit_file_node_strips_markdown_fences(self, mock_query):
        mock_query.return_value = "```python\ndef add(a, b):\n    return a + b\n```"

        state: rw.AgentState = {
            "issue_number": 1,
            "issue_body": "Fix addition bug",
            "target_files": ["calc.py"],
            "verification_cmd": "pytest",
            "file_contents": {"calc.py": "def add(a, b): return a - b"},
            "current_errors": "",
            "retry_count": 0,
            "ast_valid": False,
            "tests_passed": False,
            "worktree_dir": "/tmp/worktree",
            "branch_name": "repair/issue-1",
        }

        updates = rw.edit_file_node(state)
        self.assertEqual(updates["file_contents"]["calc.py"], "def add(a, b):\n    return a + b")
        mock_query.assert_called_once()

    @patch("scripts.repair_worker._query_ollama")
    def test_edit_file_node_truncates_large_error_context(self, mock_query):
        mock_query.return_value = "```python\ndef add(a, b):\n    return a + b\n```"

        large_error = "\n".join([f"Line error {i}" for i in range(200)])
        state: rw.AgentState = {
            "issue_number": 1,
            "issue_body": "Fix addition bug",
            "target_files": ["calc.py"],
            "verification_cmd": "pytest",
            "file_contents": {"calc.py": "def add(a, b): return a - b"},
            "current_errors": large_error,
            "retry_count": 1,
            "ast_valid": False,
            "tests_passed": False,
            "worktree_dir": "/tmp/worktree",
            "branch_name": "repair/issue-1",
        }

        rw.edit_file_node(state)
        mock_query.assert_called_once()
        sent_prompt = mock_query.call_args[0][0]
        self.assertIn("Line error 199", sent_prompt)
        self.assertNotIn("Line error 0\n", sent_prompt)

    def test_ast_check_node_valid(self):
        temp_dir = tempfile.mkdtemp()
        try:
            state: rw.AgentState = {
                "issue_number": 1,
                "issue_body": "",
                "target_files": ["module.py"],
                "verification_cmd": "",
                "file_contents": {"module.py": "def hello():\n    return 'world'\n"},
                "current_errors": "",
                "retry_count": 0,
                "ast_valid": False,
                "tests_passed": False,
                "worktree_dir": temp_dir,
                "branch_name": "repair/issue-1",
            }

            updates = rw.ast_check_node(state)
            self.assertTrue(updates["ast_valid"])
            self.assertEqual(updates["current_errors"], "")

            written_file = os.path.join(temp_dir, "module.py")
            self.assertTrue(os.path.exists(written_file))
            with open(written_file, "r") as f:
                self.assertEqual(f.read(), "def hello():\n    return 'world'\n")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_ast_check_node_invalid(self):
        state: rw.AgentState = {
            "issue_number": 1,
            "issue_body": "",
            "target_files": ["module.py"],
            "verification_cmd": "",
            "file_contents": {"module.py": "def hello(:\n return world\n"},
            "current_errors": "",
            "retry_count": 0,
            "ast_valid": False,
            "tests_passed": False,
            "worktree_dir": "/tmp/worktree",
            "branch_name": "repair/issue-1",
        }

        updates = rw.ast_check_node(state)
        self.assertFalse(updates["ast_valid"])
        self.assertIn("AST Pre-validation Failed", updates["current_errors"])
        self.assertEqual(updates["retry_count"], 1)

    @patch("subprocess.run")
    def test_verify_code_node_pass(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "OK"
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        state: rw.AgentState = {
            "issue_number": 1,
            "issue_body": "",
            "target_files": ["module.py"],
            "verification_cmd": "pytest",
            "file_contents": {},
            "current_errors": "",
            "retry_count": 0,
            "ast_valid": True,
            "tests_passed": False,
            "worktree_dir": "/tmp/worktree",
            "branch_name": "repair/issue-1",
        }

        updates = rw.verify_code_node(state)
        self.assertTrue(updates["tests_passed"])
        self.assertEqual(updates["current_errors"], "")
        mock_run.assert_called_once_with(
            ["pytest"],
            cwd="/tmp/worktree",
            shell=False,
            stdout=rw.subprocess.PIPE,
            stderr=rw.subprocess.PIPE,
            text=True,
        )

    @patch("subprocess.run")
    def test_verify_code_node_fail(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = "FAILED"
        mock_proc.stderr = "AssertionError: 1 != 2"
        mock_run.return_value = mock_proc

        state: rw.AgentState = {
            "issue_number": 1,
            "issue_body": "",
            "target_files": ["module.py"],
            "verification_cmd": "pytest",
            "file_contents": {},
            "current_errors": "",
            "retry_count": 1,
            "ast_valid": True,
            "tests_passed": False,
            "worktree_dir": "/tmp/worktree",
            "branch_name": "repair/issue-1",
        }

        updates = rw.verify_code_node(state)
        self.assertFalse(updates["tests_passed"])
        self.assertIn("Verification command failed", updates["current_errors"])
        self.assertIn("AssertionError: 1 != 2", updates["current_errors"])
        self.assertEqual(updates["retry_count"], 2)

    @patch("subprocess.run")
    def test_create_pr_node(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "https://github.com/org/repo/pull/123"
        mock_run.return_value = mock_proc

        state: rw.AgentState = {
            "issue_number": 7,
            "issue_body": "",
            "target_files": ["foo.py"],
            "verification_cmd": "pytest",
            "file_contents": {},
            "current_errors": "",
            "retry_count": 0,
            "ast_valid": True,
            "tests_passed": True,
            "worktree_dir": "/tmp/worktree",
            "branch_name": "repair/issue-7",
        }

        updates = rw.create_pr_node(state)
        self.assertEqual(updates, {})
        # Verify calls to git add, git commit, git push, gh pr create
        expected_calls = [
            call(["git", "add", "-A"], cwd="/tmp/worktree", check=True),
            call(["git", "commit", "-m", "fix(repair): autonomous repair for issue #7"], cwd="/tmp/worktree", check=True),
            call(["git", "push", "origin", "repair/issue-7"], cwd="/tmp/worktree", check=True),
            call(
                [
                    "gh",
                    "pr",
                    "create",
                    "--title",
                    "fix(repair): autonomous repair for issue #7",
                    "--body",
                    "Autonomous code repair resolved issue #7.\n\nTarget Files: foo.py\nVerification Command: `pytest`\n",
                    "--head",
                    "repair/issue-7",
                ],
                cwd="/tmp/worktree",
                stdout=rw.subprocess.PIPE,
                stderr=rw.subprocess.PIPE,
                text=True,
            ),
        ]
        mock_run.assert_has_calls(expected_calls)

    def test_conditional_routing(self):
        # Routing after AST check
        # Invalid AST, retry < 3
        s1 = {"ast_valid": False, "retry_count": 1}
        self.assertEqual(rw.route_after_ast_check(s1), "edit_file_node")

        # Invalid AST, retry >= 3
        s2 = {"ast_valid": False, "retry_count": 3}
        self.assertEqual(rw.route_after_ast_check(s2), "cleanup_node")

        # Valid AST
        s3 = {"ast_valid": True, "retry_count": 1}
        self.assertEqual(rw.route_after_ast_check(s3), "verify_code_node")

        # Routing after verify
        # Tests passed
        s4 = {"tests_passed": True, "retry_count": 1}
        self.assertEqual(rw.route_after_verify(s4), "create_pr_node")

        # Tests failed, retry < 3
        s5 = {"tests_passed": False, "retry_count": 2}
        self.assertEqual(rw.route_after_verify(s5), "edit_file_node")

        # Tests failed, retry >= 3
        s6 = {"tests_passed": False, "retry_count": 3}
        self.assertEqual(rw.route_after_verify(s6), "cleanup_node")

    @unittest.skipIf(rw.StateGraph is None, "langgraph is not installed")
    @patch("scripts.repair_worker.create_pr_node")
    @patch("scripts.repair_worker.verify_code_node")
    @patch("scripts.repair_worker.ast_check_node")
    @patch("scripts.repair_worker.edit_file_node")
    @patch("scripts.repair_worker.setup_worktree_node")
    @patch("scripts.repair_worker.parse_issue_node")
    def test_full_graph_progression_happy_path(
        self,
        mock_parse,
        mock_setup,
        mock_edit,
        mock_ast,
        mock_verify,
        mock_pr,
    ):
        mock_parse.return_value = {
            "issue_body": self.sample_task_spec,
            "target_files": ["src/calculator.py"],
            "verification_cmd": "python3 -m unittest",
        }
        mock_setup.return_value = {
            "worktree_dir": "/tmp/mock_worktree",
            "branch_name": "repair/issue-42",
            "file_contents": {"src/calculator.py": "def add(): pass"},
        }
        mock_edit.return_value = {
            "file_contents": {"src/calculator.py": "def add(a, b): return a + b"},
        }
        mock_ast.return_value = {
            "ast_valid": True,
            "current_errors": "",
        }
        mock_verify.return_value = {
            "tests_passed": True,
            "current_errors": "",
        }
        mock_pr.return_value = {}

        graph = rw.build_repair_graph()
        initial_state: rw.AgentState = {
            "issue_number": 42,
            "issue_body": self.sample_task_spec,
            "target_files": [],
            "verification_cmd": "",
            "file_contents": {},
            "current_errors": "",
            "retry_count": 0,
            "ast_valid": False,
            "tests_passed": False,
            "worktree_dir": "",
            "branch_name": "",
        }

        final_state = graph.invoke(initial_state)
        self.assertTrue(final_state["tests_passed"])
        mock_parse.assert_called_once()
        mock_setup.assert_called_once()
        mock_edit.assert_called_once()
        mock_ast.assert_called_once()
        mock_verify.assert_called_once()
        mock_pr.assert_called_once()

    @unittest.skipIf(rw.StateGraph is None, "langgraph is not installed")
    @patch("scripts.repair_worker.create_pr_node")
    @patch("scripts.repair_worker.verify_code_node")
    @patch("scripts.repair_worker.ast_check_node")
    @patch("scripts.repair_worker.edit_file_node")
    @patch("scripts.repair_worker.setup_worktree_node")
    @patch("scripts.repair_worker.parse_issue_node")
    def test_full_graph_loop_routing_on_ast_failure(
        self,
        mock_parse,
        mock_setup,
        mock_edit,
        mock_ast,
        mock_verify,
        mock_pr,
    ):
        mock_parse.return_value = {
            "issue_body": self.sample_task_spec,
            "target_files": ["src/calculator.py"],
            "verification_cmd": "python3 -m unittest",
        }
        mock_setup.return_value = {
            "worktree_dir": "/tmp/mock_worktree",
            "branch_name": "repair/issue-42",
            "file_contents": {"src/calculator.py": "def add(): pass"},
        }
        mock_edit.return_value = {
            "file_contents": {"src/calculator.py": "def add(a, b): return a + b"},
        }

        # First ast check fails, second ast check passes
        ast_calls = 0

        def fake_ast(state):
            nonlocal ast_calls
            ast_calls += 1
            if ast_calls == 1:
                return {
                    "ast_valid": False,
                    "current_errors": "SyntaxError at line 1",
                    "retry_count": state.get("retry_count", 0) + 1,
                }
            return {
                "ast_valid": True,
                "current_errors": "",
            }

        mock_ast.side_effect = fake_ast
        mock_verify.return_value = {
            "tests_passed": True,
            "current_errors": "",
        }
        mock_pr.return_value = {}

        graph = rw.build_repair_graph()
        initial_state: rw.AgentState = {
            "issue_number": 42,
            "issue_body": self.sample_task_spec,
            "target_files": [],
            "verification_cmd": "",
            "file_contents": {},
            "current_errors": "",
            "retry_count": 0,
            "ast_valid": False,
            "tests_passed": False,
            "worktree_dir": "",
            "branch_name": "",
        }

        final_state = graph.invoke(initial_state)
        self.assertTrue(final_state["tests_passed"])
        self.assertEqual(ast_calls, 2)
        self.assertEqual(mock_edit.call_count, 2)
        mock_verify.assert_called_once()
        mock_pr.assert_called_once()

    @unittest.skipIf(rw.StateGraph is None, "langgraph is not installed")
    @patch("scripts.repair_worker.create_pr_node")
    @patch("scripts.repair_worker.verify_code_node")
    @patch("scripts.repair_worker.ast_check_node")
    @patch("scripts.repair_worker.edit_file_node")
    @patch("scripts.repair_worker.setup_worktree_node")
    @patch("scripts.repair_worker.parse_issue_node")
    def test_full_graph_loop_routing_on_test_failure_and_max_retries(
        self,
        mock_parse,
        mock_setup,
        mock_edit,
        mock_ast,
        mock_verify,
        mock_pr,
    ):
        mock_parse.return_value = {
            "issue_body": self.sample_task_spec,
            "target_files": ["src/calculator.py"],
            "verification_cmd": "python3 -m unittest",
        }
        mock_setup.return_value = {
            "worktree_dir": "/tmp/mock_worktree",
            "branch_name": "repair/issue-42",
            "file_contents": {"src/calculator.py": "def add(): pass"},
        }
        mock_edit.return_value = {
            "file_contents": {"src/calculator.py": "def add(a, b): return a + b"},
        }
        mock_ast.return_value = {
            "ast_valid": True,
            "current_errors": "",
        }

        # Tests always fail until max retries reached
        def fake_verify(state):
            return {
                "tests_passed": False,
                "current_errors": "Test failed: AssertionError",
                "retry_count": state.get("retry_count", 0) + 1,
            }

        mock_verify.side_effect = fake_verify

        graph = rw.build_repair_graph()
        initial_state: rw.AgentState = {
            "issue_number": 42,
            "issue_body": self.sample_task_spec,
            "target_files": [],
            "verification_cmd": "",
            "file_contents": {},
            "current_errors": "",
            "retry_count": 0,
            "ast_valid": False,
            "tests_passed": False,
            "worktree_dir": "",
            "branch_name": "",
        }

        final_state = graph.invoke(initial_state)
        self.assertFalse(final_state["tests_passed"])
        self.assertEqual(final_state["retry_count"], 3)
        self.assertEqual(mock_verify.call_count, 3)
        self.assertEqual(mock_edit.call_count, 3)
        mock_pr.assert_not_called()


if __name__ == "__main__":
    unittest.main()
