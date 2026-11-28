#!/usr/bin/env python3
"""Unit tests for deterministic task specification parser and auto-scaffolding."""

import tempfile
import unittest
from pathlib import Path

from backend.parse_task_spec import (
    SCAFFOLD_HINT,
    TaskSpecError,
    derive_verification_command,
    extract_target_files,
    format_scaffold_sections,
    parse_task_spec,
    scaffold_task_spec,
)


class TestParseTaskSpec(unittest.TestCase):
    """Test suite for parse_task_spec."""

    def test_parse_standard_template(self):
        markdown = """---
name: Deterministic Task
---

## Target Files
- `src/utils.py`
- scripts/worker.py

## Verification Command
```bash
python3 -m unittest discover -s tests -p 'test_utils.py'
```

## Required Changes
1. Update `add()` function to handle negative numbers.
2. Fix logging output.

## Expected Output / Constraints
Zero regressions. All tests must pass with returncode 0.
"""
        parsed = parse_task_spec(markdown)
        self.assertEqual(parsed["target_files"], ["src/utils.py", "scripts/worker.py"])
        self.assertEqual(
            parsed["verification_command"],
            "python3 -m unittest discover -s tests -p 'test_utils.py'",
        )
        self.assertEqual(
            parsed["required_changes"],
            [
                "Update `add()` function to handle negative numbers.",
                "Fix logging output.",
            ],
        )
        self.assertEqual(
            parsed["expected_output"],
            "Zero regressions. All tests must pass with returncode 0.",
        )

    def test_whitespace_and_backtick_variations(self):
        markdown = """
# Target Files:
  *   `path/to/first.py`
  * path/to/second.py

### Verification Command:
`bash ./scripts/run-checks.sh --all`

## Required Changes
- Change A
- Change B
"""
        parsed = parse_task_spec(markdown)
        self.assertEqual(
            parsed["target_files"], ["path/to/first.py", "path/to/second.py"]
        )
        self.assertEqual(
            parsed["verification_command"], "bash ./scripts/run-checks.sh --all"
        )
        self.assertEqual(parsed["required_changes"], ["Change A", "Change B"])
        self.assertEqual(parsed["expected_output"], "")

    def test_missing_sections_raise_task_spec_error(self):
        self.assertTrue(issubclass(TaskSpecError, ValueError))
        for body in (
            "just prose, no sections at all",
            "## Verification Command\n`make test`\n",
            "## Target Files\n- file.py\n",
        ):
            with self.assertRaises(TaskSpecError, msg=body[:30]):
                parse_task_spec(body)

    def test_escaping_targets_rejected(self):
        spec = """---
name: Deterministic Task
---

## Target Files
- `../../etc/passwd`

## Verification Command
```bash
pytest tests/
```
"""
        with self.assertRaises(ValueError):
            parse_task_spec(spec)

    def test_scaffold_hint_names_template(self):
        self.assertIn("deterministic-task.md", SCAFFOLD_HINT)
        self.assertIn("Target Files", SCAFFOLD_HINT)
        self.assertIn("Verification Command", SCAFFOLD_HINT)


class TestAutoScaffolding(unittest.TestCase):
    """Test suite for extract_target_files, derive_verification_command, and scaffold_task_spec."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.repo_dir = Path(self.tmpdir.name)
        (self.repo_dir / "src").mkdir()
        (self.repo_dir / "tests").mkdir()
        (self.repo_dir / "src" / "feature.py").write_text("# code", encoding="utf-8")
        (self.repo_dir / "tests" / "test_feature.py").write_text(
            "# test", encoding="utf-8"
        )

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_extract_target_files_existing_files(self):
        body = """
        We need to fix the bug in `src/feature.py`.
        Also consider non_existent/file.py if needed.
        """
        targets = extract_target_files(body, self.repo_dir)
        self.assertEqual(targets, ["src/feature.py"])

    def test_extract_target_files_no_matches(self):
        body = "Some general issue description with no files mentioned."
        targets = extract_target_files(body, self.repo_dir)
        self.assertEqual(targets, [])

    def test_derive_verification_command_from_explicit_body_command(self):
        body = """
        Run tests with:
        ```bash
        pytest tests/test_feature.py
        ```
        """
        cmd = derive_verification_command(body, self.repo_dir, ["src/feature.py"])
        self.assertEqual(cmd, "pytest tests/test_feature.py")

    def test_derive_verification_command_from_matching_test_file(self):
        body = "Please update `src/feature.py` to fix edge case."
        cmd = derive_verification_command(body, self.repo_dir, ["src/feature.py"])
        self.assertEqual(cmd, "pytest tests/test_feature.py")

    def test_derive_verification_command_fallback_to_repo_tests(self):
        (self.repo_dir / "src" / "other.py").write_text("# other", encoding="utf-8")
        cmd = derive_verification_command(
            "Just update `src/other.py`", self.repo_dir, ["src/other.py"]
        )
        self.assertEqual(cmd, "pytest tests/")

    def test_scaffold_task_spec_success(self):
        body = "Bug reported in `src/feature.py`."
        result, err = scaffold_task_spec(body, self.repo_dir)
        self.assertIsNone(err)
        self.assertIsNotNone(result)
        self.assertEqual(result["target_files"], ["src/feature.py"])
        self.assertEqual(result["verification_command"], "pytest tests/test_feature.py")

    def test_scaffold_task_spec_no_targets_returns_error(self):
        body = "Bug with no files."
        result, err = scaffold_task_spec(body, self.repo_dir)
        self.assertIsNone(result)
        self.assertIn("no target files found", err)

    def test_format_scaffold_sections(self):
        formatted = format_scaffold_sections(
            ["src/feature.py"], "pytest tests/test_feature.py"
        )
        self.assertIn("## Target Files\n- `src/feature.py`", formatted)
        self.assertIn(
            "## Verification Command\n\n```\npytest tests/test_feature.py\n```",
            formatted,
        )
