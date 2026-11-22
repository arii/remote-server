import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import coder
import issue_generator
from backend.shared import s5tools


class TestVerificationPolicy(unittest.TestCase):
    def test_commands_use_existing_allowlist(self):
        for command in (
            "python3 -m unittest backend.tests.test_repair_worker_fetch",
            "python3 -m unittest tests.test_deep_chat",
            "python3 -m unittest tests.test_deep_chat.TestHistoryTrim",
            "python3 -m unittest tests.test_deep_chat.TestHistoryTrim.test_trim",
            "python3 -m pytest backend/tests/test_repair_worker_fetch.py",
            "python3 -m pytest tests/test_deep_chat.py",
            "python3 tests/test_deep_chat.py",
            "python3 backend/tests/test_repair_worker_fetch.py",
        ):
            self.assertTrue(s5tools.check_allowed(s5tools.parse_check_command(command)))
        for command in (
            "npm run test:backend",
            "npm install",
            "pnpm install --ignore-scripts",
            "python3 -m unittest discover -s backend/tests",
            "python3 -m unittest os.system",
        ):
            with self.assertRaises(ValueError):
                s5tools.parse_check_command(command)

    def test_verification_env_prepends_cur_bin_to_path(self):
        import os
        import sys

        cur_bin = os.path.dirname(sys.executable)
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
            with s5tools.verification_env("/checkout") as env:
                self.assertTrue(
                    env["PATH"].startswith(cur_bin + os.pathsep)
                    or env["PATH"] == cur_bin
                )

    def test_coder_and_both_planners_get_canonical_policy(self):
        policy = s5tools.verification_guidance()
        self.assertIn(policy, coder.build_system("/checkout", "task"))
        self.assertIn(policy, coder.PLAN_PROMPT.format(brief="task"))
        model = mock.Mock()
        model.invoke.return_value = "1. Fix [files: backend/repair_worker.py]"
        with mock.patch.object(coder, "build_model", return_value=(model, {})):
            coder._default_plan_fn("task", "/checkout", {})
        self.assertIn(policy, model.invoke.call_args.args[0])
        self.assertNotIn("pnpm install", policy)

    def test_spec_template_requires_injected_policy(self):
        skill = Path(issue_generator.__file__).parent / "skills/write_spec_issue.md"
        text = skill.read_text(encoding="utf-8")
        self.assertIn("accepted by the injected run_check verification policy", text)
        self.assertNotIn("project_config.json", text)

    def test_coder_tool_executes_allowlisted_python_command(self):
        try:
            import langchain_core  # noqa: F401
        except ImportError:
            self.skipTest("agent dependencies not installed")
        with tempfile.TemporaryDirectory() as root:
            with mock.patch(
                "subprocess.run",
                return_value=types.SimpleNamespace(
                    returncode=0, stdout="OK", stderr=""
                ),
            ) as run:
                result = coder.build_check_tool(root).invoke(
                    {
                        "command": "python3 -m unittest backend.tests.test_repair_worker_fetch"
                    }
                )
            self.assertEqual(result, "exit=0\nOK")
            self.assertEqual(
                run.call_args.args[0],
                ["python3", "-m", "unittest", "backend.tests.test_repair_worker_fetch"],
            )
            self.assertEqual(run.call_args.kwargs["cwd"], root)


class TestFullDraftPreservation(unittest.TestCase):
    def draft(self, content):
        model = mock.Mock()
        model.invoke.return_value = types.SimpleNamespace(content=content)
        graph = issue_generator.IssueGeneratorGraph("/checkout", model_fn=lambda: model)
        draft = graph.draft_text("task", "", [], {})
        self.assertIn(s5tools.verification_guidance(), model.invoke.call_args.args[0])
        return draft

    def test_long_acceptance_criteria_survive_drafting_and_serialization(self):
        content = (
            "## Problem Statement\n"
            + "Full context.\n" * 600
            + "## Acceptance Criteria\n- [ ] The function `ollama_tags_url()` is still present and correctly defined.\n"
        )
        draft = self.draft(content)
        self.assertEqual(draft, content)
        self.assertEqual(issue_generator.completed_spec({"draft": draft}), content)

    def test_incomplete_model_output_is_not_created_by_cleanup(self):
        content = "## Acceptance Criteria\n- [ ] The function `ollama_tags\n"
        self.assertEqual(self.draft(content), content)
        self.assertEqual(issue_generator.completed_spec({"draft": content}), content)

    def test_empty_model_output(self):
        self.assertEqual(self.draft(""), "")

    def test_model_error_propagates(self):
        model = mock.Mock()
        model.invoke.side_effect = ValueError("model failed")
        graph = issue_generator.IssueGeneratorGraph("/checkout", model_fn=lambda: model)
        with self.assertRaisesRegex(ValueError, "model failed"):
            graph.draft_text("task", "", [], {})
