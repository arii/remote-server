import json
import shutil
import tempfile
import unittest
from pathlib import Path

import issue_generator


class TestExplicitScope(unittest.TestCase):
    @unittest.skipUnless(shutil.which("td"), "td CLI not installed")
    def test_real_td_scope_is_explicit_and_normalized(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "backend").mkdir()
            Path(root, "backend/repair_worker.py").write_text("pass\n")
            graph = issue_generator.IssueGeneratorGraph(root)
            for path, expected in (
                ("./backend/repair_worker.py", True),
                ("missing.py", False),
            ):
                text = (
                    "## Problem Statement\nDo not change /api/, CI/ or compiler/.\n"
                    "## Goal\nFix the helper.\n## Scope\n### Allowed Files\n"
                    f"- `{path}`\n## Definition of Done\nHelper is called.\n"
                    "## Acceptance Criteria\n- [ ] Helper is called.\n"
                    "## Verification Commands\n```bash\npython3 -m pytest tests/\n```\n"
                )
                valid, data, errors = graph.validate_spec(text)
                self.assertEqual(valid, expected, errors)
                self.assertEqual(data["allowed_files"], [path.removeprefix("./")])
                self.assertEqual(
                    data["verification_commands"], ["python3 -m pytest tests/"]
                )

    def test_empty_and_unsafe_scope(self):
        self.assertEqual(issue_generator.explicit_allowed_paths(""), [])
        graph = issue_generator.IssueGeneratorGraph(
            "/tmp",
            td_runner=lambda args: (0, json.dumps({"valid": True, "errors": []})),
        )
        for path in ("/api/", "../outside.py"):
            valid, _, errors = graph.validate_spec(f"### Allowed Files\n- `{path}`\n")
            self.assertFalse(valid)
            self.assertIn("repo-relative", errors[0])

    def test_non_scope_errors_remain_blocking(self):
        graph = issue_generator.IssueGeneratorGraph(
            "/tmp",
            td_runner=lambda args: (
                1,
                json.dumps(
                    {"valid": False, "errors": ["missing required section: Goal"]}
                ),
            ),
        )
        self.assertEqual(graph.validate_spec("")[2], ["missing required section: Goal"])
