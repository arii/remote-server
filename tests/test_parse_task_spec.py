#!/usr/bin/env python3
"""Unit tests for deterministic task specification parser."""

import os
import unittest
from scripts.parse_task_spec import parse_task_spec


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

    def test_parse_template_file(self):
        template_path = os.path.join(
            os.path.dirname(__file__), "..", ".github", "ISSUE_TEMPLATE", "deterministic-task.md"
        )
        self.assertTrue(os.path.exists(template_path))
        with open(template_path, "r", encoding="utf-8") as f:
            template_content = f.read()

        # The raw issue template itself has empty placeholder items under Target Files / Verification Command,
        # so attempting to parse the unfilled template should raise a ValueError.
        with self.assertRaises(ValueError):
            parse_task_spec(template_content)

        # Once filled in, it parses cleanly
        filled = template_content.replace(
            "-\n", "- `scripts/foo.py`\n"
        ).replace(
            "```bash\n\n```", "```bash\npytest tests/\n```"
        )
        res = parse_task_spec(filled)
        self.assertEqual(res["target_files"], ["scripts/foo.py"])
        self.assertEqual(res["verification_command"], "pytest tests/")

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
        self.assertEqual(parsed["target_files"], ["path/to/first.py", "path/to/second.py"])
        self.assertEqual(parsed["verification_command"], "bash ./scripts/run-checks.sh --all")
        self.assertEqual(parsed["required_changes"], ["Change A", "Change B"])
        self.assertEqual(parsed["expected_output"], "")

    def test_extra_sections_ignored_or_preserved(self):
        markdown = """
## Background & Objective
Some background context that should not break parsing.

## Target Files
- `main.go`

## Verification Command
```sh
go test ./...
```

## Notes
Extra note section here.
"""
        parsed = parse_task_spec(markdown)
        self.assertEqual(parsed["target_files"], ["main.go"])
        self.assertEqual(parsed["verification_command"], "go test ./...")
        self.assertEqual(parsed["required_changes"], [])
        self.assertEqual(parsed["expected_output"], "")

    def test_missing_target_files_raises(self):
        markdown = """
## Verification Command
```bash
make test
```
"""
        with self.assertRaises(ValueError) as ctx:
            parse_task_spec(markdown)
        self.assertIn("Target Files", str(ctx.exception))

    def test_empty_target_files_raises(self):
        markdown = """
## Target Files

<!-- just comments -->

## Verification Command
```bash
make test
```
"""
        with self.assertRaises(ValueError) as ctx:
            parse_task_spec(markdown)
        self.assertIn("Target Files", str(ctx.exception))

    def test_missing_verification_command_raises(self):
        markdown = """
## Target Files
- file.py
"""
        with self.assertRaises(ValueError) as ctx:
            parse_task_spec(markdown)
        self.assertIn("Verification Command", str(ctx.exception))

    def test_empty_verification_command_raises(self):
        markdown = """
## Target Files
- file.py

## Verification Command
```bash

```
"""
        with self.assertRaises(ValueError) as ctx:
            parse_task_spec(markdown)
        self.assertIn("Verification Command", str(ctx.exception))

    def test_invalid_input_type(self):
        with self.assertRaises(ValueError):
            parse_task_spec(None)  # type: ignore


if __name__ == "__main__":
    unittest.main()
