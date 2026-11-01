#!/usr/bin/env python3
"""Unit tests for the autonomous local repair worker."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from scripts.repair_worker import (
    GitWorktreeManager,
    RepairWorker,
    apply_edits_to_worktree,
    format_conventional_commit,
    parse_edits_from_response,
)


class TestRepairWorker(unittest.TestCase):
    """Test suite for local repair worker utilities and loop."""

    def test_parse_edits_merge_diff(self):
        llm_output = """Here is the fix:
File: `src/utils.py`
<<<<<<< SEARCH
def add(a, b):
    return a - b
=======
def add(a, b):
    return a + b
>>>>>>> REPLACE
"""
        edits = parse_edits_from_response(llm_output)
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["file"], "src/utils.py")
        self.assertEqual(edits[0]["type"], "replace_block")
        self.assertIn("return a - b", edits[0]["search"])
        self.assertIn("return a + b", edits[0]["replace"])

    def test_parse_edits_fenced_block(self):
        llm_output = """Here is the full fixed file:
File: `src/calc.py`
```python
def multiply(a, b):
    return a * b
```
"""
        edits = parse_edits_from_response(llm_output)
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["file"], "src/calc.py")
        self.assertEqual(edits[0]["type"], "whole_file")
        self.assertIn("def multiply(a, b):", edits[0]["content"])

    def test_apply_edits_ast_validation_failure(self):
        temp_dir = tempfile.mkdtemp()
        try:
            # Create a bad python edit with syntax error
            edits = [
                {
                    "file": "broken.py",
                    "type": "whole_file",
                    "content": "def broken_func(\n    invalid syntax here!! ))\n",
                }
            ]
            applied, msg = apply_edits_to_worktree(temp_dir, edits)
            self.assertFalse(applied)
            self.assertIn("AST/Syntax validation error", msg)
        finally:
            shutil.rmtree(temp_dir)

    def test_apply_edits_valid_python(self):
        temp_dir = tempfile.mkdtemp()
        try:
            edits = [
                {
                    "file": "calc.py",
                    "type": "whole_file",
                    "content": "def add(a: int, b: int) -> int:\n    return a + b\n",
                }
            ]
            applied, msg = apply_edits_to_worktree(temp_dir, edits)
            self.assertTrue(applied)
            with open(os.path.join(temp_dir, "calc.py")) as f:
                content = f.read()
            self.assertIn("return a + b", content)
        finally:
            shutil.rmtree(temp_dir)

    def test_format_conventional_commit(self):
        spec = {
            "target_files": ["scripts/repair_worker.py"],
            "verification_command": "python3 -m unittest",
            "required_changes": ["Fix issue in parse_task_spec"],
        }
        msg = format_conventional_commit(spec, issue_number=42)
        self.assertTrue(msg.startswith("fix(repair_worker): Fix issue in parse_task_spec"))
        self.assertIn("Closes #42", msg)
        self.assertIn("Verified with: `python3 -m unittest`", msg)


if __name__ == "__main__":
    unittest.main()
