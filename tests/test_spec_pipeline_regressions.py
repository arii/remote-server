import json
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import issue_generator
import pipeline


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


class TestSpecResume(unittest.TestCase):
    @unittest.skipUnless(
        importlib.util.find_spec("langgraph"), "langgraph not installed"
    )
    def test_real_spec_interrupt_resumes_with_answers(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "widget.py").write_text("pass\n")
            pipe = pipeline.Pipeline(
                root,
                "fix widget",
                spec_model_fn=lambda: object(),
                investigate_fn=lambda *args: "widget.py:1 evidence",
                td_run=lambda *args: (
                    0,
                    json.dumps({"status": "ready", "valid": True}),
                ),
            )
            question = "Keep the helper?"

            def answer(interrupts, state):
                self.assertEqual(
                    pipeline.final_interrupt(interrupts)["questions"], [question]
                )
                return {question: "yes"}

            pipe._spec_policy = answer
            with (
                mock.patch.object(
                    issue_generator.IssueGeneratorGraph,
                    "draft_text",
                    return_value=f"### Allowed Files\n- `widget.py`\n- {question}",
                ),
                mock.patch("ledger.record"),
            ):
                ok, state, text = pipe.stage_spec()
            self.assertTrue(ok, text)
            self.assertEqual(state["brief"], "fix widget")
            self.assertEqual(state["answers"][question], "yes")
            self.assertIn("yes", text)


class TestFailureDetail(unittest.TestCase):
    def test_spec_failures_keep_their_reason_in_result(self):
        for state, detail, expected in (
            ({}, "spec drive exhausted", "spec drive exhausted"),
            ({"errors": ["missing scope"]}, "invalid spec", "missing scope"),
            ({"errors": []}, ["invalid spec"], "invalid spec"),
            ({}, "", "spec failed"),
        ):
            with self.subTest(state=state, detail=detail):
                pipe = pipeline.Pipeline("/tmp", "task")
                with (
                    mock.patch.object(pipe, "stage_init", return_value=(True, {})),
                    mock.patch.object(
                        pipe, "stage_spec", return_value=(False, state, detail)
                    ),
                ):
                    result = pipe.run()
                self.assertEqual(result["detail"], expected)
                self.assertEqual(result["status"], pipeline.BLOCKED)
