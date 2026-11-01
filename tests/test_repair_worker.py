#!/usr/bin/env python3
"""Unit tests for the repair worker state machine and helper modules."""

import os
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

# Ensure repo root and scripts are importable
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from scripts.ast_validator import validate_source_syntax
from scripts.parse_task_spec import parse_task_spec
from scripts.repair_worker import (
    AgentState,
    ast_check_node,
    build_repair_graph,
    cleanup_node,
    create_pr_node,
    edit_file_node,
    extract_code_fence,
    parse_issue_node,
    query_ollama,
    route_ast_check,
    route_verification,
    run_repair_worker,
    sanitize_relative_path,
    setup_worktree_node,
    verify_code_node,
)


class TestTaskSpecParser(unittest.TestCase):
    def test_parse_task_spec_standard(self):
        body = """
Target Files
scripts/repair_worker.py
tests/test_repair_worker.py

Verification Command
python3 -m unittest discover -s tests -p 'test_repair_worker.py'
"""
        spec = parse_task_spec(body)
        self.assertEqual(spec["target_files"], ["scripts/repair_worker.py", "tests/test_repair_worker.py"])
        self.assertEqual(
            spec["verification_cmd"],
            "python3 -m unittest discover -s tests -p 'test_repair_worker.py'",
        )

    def test_parse_task_spec_with_markdown_fences(self):
        body = """
### Target Files:
- `lib/core.py`
- `tests/test_core.py`

### Verification Command:
```bash
pytest tests/test_core.py
```
"""
        spec = parse_task_spec(body)
        self.assertEqual(spec["target_files"], ["lib/core.py", "tests/test_core.py"])
        self.assertEqual(spec["verification_cmd"], "pytest tests/test_core.py")

    def test_parse_task_spec_fallback(self):
        body = "Target Files: foo.py, bar.py\nVerification Command: `pytest`"
        spec = parse_task_spec(body)
        self.assertEqual(spec["target_files"], ["foo.py", "bar.py"])
        self.assertEqual(spec["verification_cmd"], "pytest")


class TestAstValidator(unittest.TestCase):
    def test_valid_python(self):
        valid, err = validate_source_syntax("example.py", "def add(a, b):\n    return a + b\n")
        self.assertTrue(valid)
        self.assertEqual(err, "")

    def test_invalid_python(self):
        valid, err = validate_source_syntax("example.py", "def add(a, b\n    return a + b")
        self.assertFalse(valid)
        self.assertIn("SyntaxError", err)

    def test_valid_json(self):
        valid, err = validate_source_syntax("config.json", '{"key": "value"}')
        self.assertTrue(valid)
        self.assertEqual(err, "")

    def test_invalid_json(self):
        valid, err = validate_source_syntax("config.json", '{"key": "value",}')
        self.assertFalse(valid)
        self.assertIn("JSONDecodeError", err)

    def test_other_files_pass_by_default(self):
        valid, err = validate_source_syntax("readme.md", "# Title\nSome text")
        self.assertTrue(valid)
        self.assertEqual(err, "")

    def test_path_traversal_detection(self):
        valid, err = validate_source_syntax("../etc/passwd", "test")
        self.assertFalse(valid)
        self.assertIn("Unsafe path traversal", err)


class TestRepairWorkerNodes(unittest.TestCase):
    def test_sanitize_relative_path(self):
        self.assertEqual(sanitize_relative_path("scripts/app.py"), "scripts/app.py")
        self.assertEqual(sanitize_relative_path("/scripts/app.py"), "scripts/app.py")
        with self.assertRaises(ValueError):
            sanitize_relative_path("../secret.py")
        with self.assertRaises(ValueError):
            sanitize_relative_path("scripts/../../secret.py")

    def test_extract_code_fence(self):
        raw = "Here is the code:\n```python\nprint('hello')\n```\nHope it helps!"
        self.assertEqual(extract_code_fence(raw, "app.py"), "print('hello')")

        plain = "print('hello plain')"
        self.assertEqual(extract_code_fence(plain, "app.py"), "print('hello plain')")

    def test_extract_code_fence_multiple_blocks(self):
        raw = (
            "Let's write instructions first:\n"
            "```text\nstep 1\n```\n"
            "Here is the real source file:\n"
            "```python\ndef full_implementation():\n    return 42\n```"
        )
        # Should pick the largest block
        self.assertEqual(
            extract_code_fence(raw, "app.py"),
            "def full_implementation():\n    return 42",
        )

    @patch("scripts.repair_worker.urllib.request.urlopen")
    def test_query_ollama(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"response": "rewritten code"}'
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        result = query_ollama("prompt", "system prompt")
        self.assertEqual(result, "rewritten code")
        self.assertTrue(mock_urlopen.called)

    def test_parse_issue_node(self):
        state: AgentState = {
            "issue_number": 42,
            "issue_body": "Target Files\nfoo.py\n\nVerification Command\npytest",
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
        res = parse_issue_node(state)
        self.assertEqual(res["target_files"], ["foo.py"])
        self.assertEqual(res["verification_cmd"], "pytest")

    @patch("scripts.repair_worker.run_cmd")
    @patch("os.path.exists")
    def test_setup_worktree_node(self, mock_exists, mock_run_cmd):
        mock_exists.return_value = False
        state: AgentState = {
            "issue_number": 42,
            "issue_body": "",
            "target_files": ["foo.py"],
            "verification_cmd": "pytest",
            "file_contents": {},
            "current_errors": "",
            "retry_count": 0,
            "ast_valid": False,
            "tests_passed": False,
            "worktree_dir": "",
            "branch_name": "",
        }
        res = setup_worktree_node(state)
        self.assertEqual(res["branch_name"], "repair/issue-42")
        self.assertIn("repair_worktree_42_", res["worktree_dir"])
        self.assertEqual(res["file_contents"], {"foo.py": ""})
        self.assertTrue(mock_run_cmd.called)

    @patch("scripts.repair_worker.query_ollama")
    @patch("builtins.open", create=True)
    @patch("os.makedirs")
    def test_edit_file_node(self, mock_makedirs, mock_open, mock_query_ollama):
        mock_query_ollama.return_value = "```python\ndef fixed(): pass\n```"
        mock_file = MagicMock()
        mock_open.return_value.__enter__.return_value = mock_file

        state: AgentState = {
            "issue_number": 42,
            "issue_body": "fix a bug",
            "target_files": ["app.py"],
            "verification_cmd": "pytest",
            "file_contents": {"app.py": "def broken(): pass"},
            "current_errors": "AssertionError",
            "retry_count": 1,
            "ast_valid": False,
            "tests_passed": False,
            "worktree_dir": "/tmp/test_wt",
            "branch_name": "repair/issue-42",
        }

        res = edit_file_node(state)
        self.assertEqual(res["retry_count"], 2)
        self.assertEqual(res["file_contents"]["app.py"], "def fixed(): pass")
        mock_query_ollama.assert_called_once()

    def test_ast_check_node_valid(self):
        state: AgentState = {
            "issue_number": 42,
            "issue_body": "",
            "target_files": ["app.py"],
            "verification_cmd": "",
            "file_contents": {"app.py": "def valid():\n    return 42\n"},
            "current_errors": "",
            "retry_count": 1,
            "ast_valid": False,
            "tests_passed": False,
            "worktree_dir": "",
            "branch_name": "",
        }
        res = ast_check_node(state)
        self.assertTrue(res["ast_valid"])
        self.assertEqual(res["current_errors"], "")

    def test_ast_check_node_invalid(self):
        state: AgentState = {
            "issue_number": 42,
            "issue_body": "",
            "target_files": ["app.py"],
            "verification_cmd": "",
            "file_contents": {"app.py": "def broken(\n"},
            "current_errors": "",
            "retry_count": 1,
            "ast_valid": True,
            "tests_passed": False,
            "worktree_dir": "",
            "branch_name": "",
        }
        res = ast_check_node(state)
        self.assertFalse(res["ast_valid"])
        self.assertIn("SyntaxError", res["current_errors"])

    @patch("scripts.repair_worker.run_cmd")
    def test_verify_code_node_success(self, mock_run_cmd):
        mock_run_cmd.return_value = subprocess.CompletedProcess(
            args=["pytest", "-v"], returncode=0, stdout="all tests passed", stderr=""
        )
        state: AgentState = {
            "issue_number": 42,
            "issue_body": "",
            "target_files": [],
            "verification_cmd": "pytest -v",
            "file_contents": {},
            "current_errors": "",
            "retry_count": 1,
            "ast_valid": True,
            "tests_passed": False,
            "worktree_dir": "/tmp/wt",
            "branch_name": "",
        }
        res = verify_code_node(state)
        self.assertTrue(res["tests_passed"])
        self.assertEqual(res["current_errors"], "")
        mock_run_cmd.assert_called_once_with(
            ["pytest", "-v"],
            cwd="/tmp/wt",
            shell=False,
            check=False,
            timeout=300,
        )

    @patch("scripts.repair_worker.run_cmd")
    def test_verify_code_node_failure(self, mock_run_cmd):
        mock_run_cmd.return_value = subprocess.CompletedProcess(
            args=["pytest"], returncode=1, stdout="", stderr="FAILED tests/test_app.py"
        )
        state: AgentState = {
            "issue_number": 42,
            "issue_body": "",
            "target_files": [],
            "verification_cmd": "pytest",
            "file_contents": {},
            "current_errors": "",
            "retry_count": 1,
            "ast_valid": True,
            "tests_passed": False,
            "worktree_dir": "/tmp/wt",
            "branch_name": "",
        }
        res = verify_code_node(state)
        self.assertFalse(res["tests_passed"])
        self.assertIn("FAILED tests/test_app.py", res["current_errors"])
        mock_run_cmd.assert_called_once_with(
            ["pytest"],
            cwd="/tmp/wt",
            shell=False,
            check=False,
            timeout=300,
        )

    @patch("scripts.repair_worker.run_cmd")
    def test_create_pr_node(self, mock_run_cmd):
        state: AgentState = {
            "issue_number": 42,
            "issue_body": "",
            "target_files": ["foo.py"],
            "verification_cmd": "pytest",
            "file_contents": {},
            "current_errors": "",
            "retry_count": 1,
            "ast_valid": True,
            "tests_passed": True,
            "worktree_dir": "/tmp/wt",
            "branch_name": "repair/issue-42",
        }
        res = create_pr_node(state)
        self.assertEqual(res, {})
        # Verify git add, commit, push, gh pr create
        self.assertEqual(mock_run_cmd.call_count, 4)

    @patch("scripts.repair_worker.run_cmd")
    @patch("shutil.rmtree")
    @patch("os.path.exists")
    def test_cleanup_node(self, mock_exists, mock_rmtree, mock_run_cmd):
        mock_exists.return_value = True
        state: AgentState = {
            "issue_number": 42,
            "issue_body": "",
            "target_files": [],
            "verification_cmd": "",
            "file_contents": {},
            "current_errors": "",
            "retry_count": 0,
            "ast_valid": True,
            "tests_passed": True,
            "worktree_dir": "/tmp/wt",
            "branch_name": "",
        }
        res = cleanup_node(state)
        self.assertEqual(res, {})
        mock_run_cmd.assert_called_once_with(["git", "worktree", "remove", "--force", "/tmp/wt"], check=False)
        mock_rmtree.assert_called_once_with("/tmp/wt", ignore_errors=True)


class TestRepairWorkerRouting(unittest.TestCase):
    def test_route_ast_check_valid(self):
        state: AgentState = {
            "issue_number": 1,
            "issue_body": "",
            "target_files": [],
            "verification_cmd": "",
            "file_contents": {},
            "current_errors": "",
            "retry_count": 1,
            "ast_valid": True,
            "tests_passed": False,
            "worktree_dir": "",
            "branch_name": "",
        }
        self.assertEqual(route_ast_check(state), "verify_code_node")

    def test_route_ast_check_invalid_loops_to_edit(self):
        state: AgentState = {
            "issue_number": 1,
            "issue_body": "",
            "target_files": [],
            "verification_cmd": "",
            "file_contents": {},
            "current_errors": "syntax error",
            "retry_count": 1,
            "ast_valid": False,
            "tests_passed": False,
            "worktree_dir": "",
            "branch_name": "",
        }
        self.assertEqual(route_ast_check(state), "edit_file_node")

    def test_route_ast_check_max_retries_exceeded(self):
        state: AgentState = {
            "issue_number": 1,
            "issue_body": "",
            "target_files": [],
            "verification_cmd": "",
            "file_contents": {},
            "current_errors": "syntax error",
            "retry_count": 3,
            "ast_valid": False,
            "tests_passed": False,
            "worktree_dir": "",
            "branch_name": "",
        }
        self.assertEqual(route_ast_check(state), "cleanup_node")

    def test_route_verification_passed(self):
        state: AgentState = {
            "issue_number": 1,
            "issue_body": "",
            "target_files": [],
            "verification_cmd": "",
            "file_contents": {},
            "current_errors": "",
            "retry_count": 1,
            "ast_valid": True,
            "tests_passed": True,
            "worktree_dir": "",
            "branch_name": "",
        }
        self.assertEqual(route_verification(state), "create_pr_node")

    def test_route_verification_failed_loops_to_edit(self):
        state: AgentState = {
            "issue_number": 1,
            "issue_body": "",
            "target_files": [],
            "verification_cmd": "",
            "file_contents": {},
            "current_errors": "test error",
            "retry_count": 2,
            "ast_valid": True,
            "tests_passed": False,
            "worktree_dir": "",
            "branch_name": "",
        }
        self.assertEqual(route_verification(state), "edit_file_node")

    def test_route_verification_max_retries_exceeded(self):
        state: AgentState = {
            "issue_number": 1,
            "issue_body": "",
            "target_files": [],
            "verification_cmd": "",
            "file_contents": {},
            "current_errors": "test error",
            "retry_count": 3,
            "ast_valid": True,
            "tests_passed": False,
            "worktree_dir": "",
            "branch_name": "",
        }
        self.assertEqual(route_verification(state), "cleanup_node")


class TestRepairWorkerEndToEnd(unittest.TestCase):
    @patch("scripts.repair_worker.cleanup_node")
    @patch("scripts.repair_worker.create_pr_node")
    @patch("scripts.repair_worker.verify_code_node")
    @patch("scripts.repair_worker.ast_check_node")
    @patch("scripts.repair_worker.edit_file_node")
    @patch("scripts.repair_worker.setup_worktree_node")
    @patch("scripts.repair_worker.parse_issue_node")
    def test_graph_happy_path(
        self,
        mock_parse,
        mock_setup,
        mock_edit,
        mock_ast,
        mock_verify,
        mock_create_pr,
        mock_cleanup,
    ):
        mock_parse.return_value = {
            "target_files": ["src/fix.py"],
            "verification_cmd": "pytest",
        }
        mock_setup.return_value = {
            "worktree_dir": "/tmp/wt_1",
            "branch_name": "repair/issue-1",
            "file_contents": {"src/fix.py": ""},
            "retry_count": 0,
            "current_errors": "",
        }
        mock_edit.return_value = {
            "file_contents": {"src/fix.py": "def test(): pass"},
            "retry_count": 1,
        }
        mock_ast.return_value = {
            "ast_valid": True,
            "current_errors": "",
        }
        mock_verify.return_value = {
            "tests_passed": True,
            "current_errors": "",
        }
        mock_create_pr.return_value = {}
        mock_cleanup.return_value = {}

        result = run_repair_worker(
            issue_number=1,
            issue_body="Target Files\nsrc/fix.py\n\nVerification Command\npytest",
        )

        mock_parse.assert_called_once()
        mock_setup.assert_called_once()
        mock_edit.assert_called_once()
        mock_ast.assert_called_once()
        mock_verify.assert_called_once()
        mock_create_pr.assert_called_once()
        mock_cleanup.assert_called_once()

        self.assertTrue(result.get("tests_passed"))
        self.assertTrue(result.get("ast_valid"))

    @patch("scripts.repair_worker.cleanup_node")
    @patch("scripts.repair_worker.create_pr_node")
    @patch("scripts.repair_worker.verify_code_node")
    @patch("scripts.repair_worker.ast_check_node")
    @patch("scripts.repair_worker.edit_file_node")
    @patch("scripts.repair_worker.setup_worktree_node")
    @patch("scripts.repair_worker.parse_issue_node")
    def test_graph_ast_failure_then_success_loop(
        self,
        mock_parse,
        mock_setup,
        mock_edit,
        mock_ast,
        mock_verify,
        mock_create_pr,
        mock_cleanup,
    ):
        mock_parse.return_value = {
            "target_files": ["src/fix.py"],
            "verification_cmd": "pytest",
        }
        mock_setup.return_value = {
            "worktree_dir": "/tmp/wt_2",
            "branch_name": "repair/issue-2",
            "file_contents": {"src/fix.py": ""},
            "retry_count": 0,
            "current_errors": "",
        }

        # Edit called twice: attempt 1 -> retry 1, attempt 2 -> retry 2
        mock_edit.side_effect = [
            {"file_contents": {"src/fix.py": "broken syntax"}, "retry_count": 1},
            {"file_contents": {"src/fix.py": "def test(): pass"}, "retry_count": 2},
        ]
        # AST check: first fails, second passes
        mock_ast.side_effect = [
            {"ast_valid": False, "current_errors": "SyntaxError"},
            {"ast_valid": True, "current_errors": ""},
        ]
        mock_verify.return_value = {
            "tests_passed": True,
            "current_errors": "",
        }
        mock_create_pr.return_value = {}
        mock_cleanup.return_value = {}

        result = run_repair_worker(issue_number=2, issue_body="fix task")

        self.assertEqual(mock_edit.call_count, 2)
        self.assertEqual(mock_ast.call_count, 2)
        mock_verify.assert_called_once()
        mock_create_pr.assert_called_once()
        mock_cleanup.assert_called_once()
        self.assertTrue(result.get("tests_passed"))

    @patch("scripts.repair_worker.cleanup_node")
    @patch("scripts.repair_worker.create_pr_node")
    @patch("scripts.repair_worker.verify_code_node")
    @patch("scripts.repair_worker.ast_check_node")
    @patch("scripts.repair_worker.edit_file_node")
    @patch("scripts.repair_worker.setup_worktree_node")
    @patch("scripts.repair_worker.parse_issue_node")
    def test_graph_test_failure_max_retries_exit(
        self,
        mock_parse,
        mock_setup,
        mock_edit,
        mock_ast,
        mock_verify,
        mock_create_pr,
        mock_cleanup,
    ):
        mock_parse.return_value = {
            "target_files": ["src/fix.py"],
            "verification_cmd": "pytest",
        }
        mock_setup.return_value = {
            "worktree_dir": "/tmp/wt_3",
            "branch_name": "repair/issue-3",
            "file_contents": {"src/fix.py": ""},
            "retry_count": 0,
            "current_errors": "",
        }

        # 3 attempts
        mock_edit.side_effect = [
            {"file_contents": {"src/fix.py": "v1"}, "retry_count": 1},
            {"file_contents": {"src/fix.py": "v2"}, "retry_count": 2},
            {"file_contents": {"src/fix.py": "v3"}, "retry_count": 3},
        ]
        # AST always valid
        mock_ast.return_value = {"ast_valid": True, "current_errors": ""}
        # Tests always fail
        mock_verify.return_value = {"tests_passed": False, "current_errors": "AssertionError"}
        mock_cleanup.return_value = {}

        result = run_repair_worker(issue_number=3, issue_body="fix task")

        self.assertEqual(mock_edit.call_count, 3)
        self.assertEqual(mock_ast.call_count, 3)
        self.assertEqual(mock_verify.call_count, 3)
        # Should NOT call create_pr_node
        mock_create_pr.assert_not_called()
        mock_cleanup.assert_called_once()
        self.assertFalse(result.get("tests_passed"))


if __name__ == "__main__":
    unittest.main()
