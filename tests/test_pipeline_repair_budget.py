import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import auditor
import coder
import pipeline


HAS_DEPS = importlib.util.find_spec("deepagents") is not None


@unittest.skipUnless(HAS_DEPS, "requires deep-agent requirements")
class TestRepairBudget(unittest.TestCase):
    def call(self, name="grep", **args):
        return SimpleNamespace(tool_call={"name": name, "args": args, "id": "probe"})

    def test_repeated_inspection_warns_then_stops_without_reexecution(self):
        from langchain_core.messages import ToolMessage

        guard = coder.repair_tool_middleware().wrap_tool_call
        handler = mock.Mock(
            return_value=ToolMessage(content="No matches found", tool_call_id="probe")
        )
        call = self.call(path="a.py", pattern="removed_helper")
        guard(call, handler)
        warning = guard(call, handler)
        self.assertIn("Already called", warning.content)
        self.assertEqual(warning.status, "error")
        guard(self.call("ls", path="."), handler)
        with self.assertRaisesRegex(coder.RepairBudgetExceeded, "repeated unchanged"):
            guard(call, handler)
        self.assertEqual(handler.call_count, 2)

    def test_successful_edit_allows_rechecking_but_failed_edit_does_not(self):
        from langchain_core.messages import ToolMessage

        for status in ("success", "error"):
            with self.subTest(status=status):
                guard = coder.repair_tool_middleware().wrap_tool_call
                handler = mock.Mock(
                    return_value=ToolMessage(
                        content="result", tool_call_id="probe", status=status
                    )
                )
                call = self.call("run_check", command="python3 -m pytest")
                guard(call, handler)
                guard(self.call("edit_file", file_path="a.py"), handler)
                result = guard(call, handler)
                self.assertEqual("Already called" in result.content, status == "error")

    def test_total_budget_cannot_be_reset_by_edits_or_unique_calls(self):
        from langchain_core.messages import ToolMessage

        guard = coder.repair_tool_middleware().wrap_tool_call
        handler = mock.Mock(
            return_value=ToolMessage(content="edited", tool_call_id="probe")
        )
        for n in range(coder.REPAIR_TOOL_LIMIT):
            guard(self.call("edit_file", file_path=f"{n}.py"), handler)
        with self.assertRaisesRegex(coder.RepairBudgetExceeded, "budget exhausted"):
            guard(self.call("read_file", file_path="next.py"), handler)
        self.assertEqual(handler.call_count, coder.REPAIR_TOOL_LIMIT)

    def test_real_agent_stops_repeated_grep_before_recursion_limit(self):
        from langchain_core.messages import AIMessage, HumanMessage
        from tests.test_coder_outcome import LocalModel

        with tempfile.TemporaryDirectory() as root:
            Path(root, "a.py").write_text("pass\n")
            model = LocalModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "grep",
                                "args": {"path": "a.py", "pattern": "removed_helper"},
                                "id": f"probe-{n}",
                                "type": "tool_call",
                            }
                        ],
                    )
                    for n in range(4)
                ]
            )
            graph = coder.build_agent(root, "repair", model=model, repair=True)
            with self.assertRaises(coder.RepairBudgetExceeded):
                graph.invoke(
                    {"messages": [HumanMessage(content="fix")]},
                    config={"recursion_limit": 100},
                )

    def test_repair_planner_and_executor_receive_findings_and_have_no_nested_retries(
        self,
    ):
        notes = json.dumps(
            {"verified": False, "findings": [{"description": "fresh tests required"}]}
        )
        planner = mock.Mock(return_value={"plan": "fix [files: a.py]"})
        with tempfile.TemporaryDirectory() as root:
            graph = pipeline.Pipeline(
                root, "fix", plan_fn=planner, answers={"choice": "keep"}
            ).make_coder(
                "### Allowed Files\n- `a.py`\n", worktree_root=root, repair_notes=notes
            )
            graph.plan_fn("task", root, {"errors": ["gate failed"]})
            context = planner.call_args.args[2]
            self.assertEqual(context["repair_notes"], notes)
            self.assertEqual(context["errors"], ["gate failed"])
            self.assertEqual(context["choice"], "keep")
            self.assertEqual((graph.max_fix_loops, graph.max_replans), (0, 0))
            with (
                mock.patch.object(
                    coder,
                    "run_coder",
                    return_value=("DONE", {}, [], 0, "ollama:test", "trace", True, 0),
                ) as executor,
                mock.patch("ledger.record"),
            ):
                graph.implement_fn("task", "plan", root, [])
            self.assertIn(notes, executor.call_args.args[1])
            self.assertTrue(executor.call_args.kwargs["repair"])
            self.assertEqual(executor.call_args.kwargs["repair_files"], ["a.py"])

    def test_model_budget_bounds_loops_even_when_tool_ids_are_reused(self):
        from langchain.agents.middleware.model_call_limit import (
            ModelCallLimitExceededError,
        )
        from langchain_core.messages import AIMessage, HumanMessage
        from tests.test_coder_outcome import LocalModel

        with tempfile.TemporaryDirectory() as root:
            model = LocalModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "ls",
                                "args": {"path": "."},
                                "id": "reused",
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            )
            graph = coder.build_agent(root, "repair", model=model, repair=True)
            with self.assertRaises(ModelCallLimitExceededError):
                graph.invoke(
                    {"messages": [HumanMessage(content="fix")]},
                    config={"recursion_limit": 100},
                )

    def test_default_planner_includes_repair_notes(self):
        model = mock.Mock()
        model.invoke.return_value = "1. Fix [files: a.py]"
        with mock.patch.object(coder, "build_model", return_value=(model, {})):
            coder._default_plan_fn("brief", "/tmp", {"repair_notes": "audit finding"})
        self.assertIn("Repair notes:\naudit finding", model.invoke.call_args.args[0])

    def test_truncated_audit_blocks_instead_of_spending_on_code_repair(self):
        findings = json.dumps(
            {
                "verified": False,
                "truncated": True,
                "findings": ["investigation repeat limit"],
            }
        )
        state = {
            "valid": True,
            "verdict": {"status": "CHANGES_REQUESTED", "findings": findings},
        }
        with (
            mock.patch.object(auditor, "AuditorGraph"),
            mock.patch.object(pipeline, "drive_to_settled", return_value=state),
        ):
            status, _, detail = pipeline.Pipeline("/tmp", "fix").stage_audit("/tmp")
        self.assertEqual(status, pipeline.BLOCKED)
        self.assertEqual(detail, findings)

    def test_first_pass_retains_normal_retry_bounds(self):
        graph = pipeline.Pipeline("/tmp", "fix").make_coder("spec")
        self.assertEqual((graph.max_fix_loops, graph.max_replans), (3, 1))


class TestChooseModelTier(unittest.TestCase):
    """Failure-class-aware tier gate (issue #1403)."""

    def test_first_pass_and_mechanical_stay_fast(self):
        for cls in (pipeline.AUDIT_SEMANTIC, pipeline.TEST_FAILURE):
            self.assertEqual(pipeline.choose_model_tier(1, cls, 0), pipeline.FAST_TIER)
        for attempt in (1, 2, 3):
            self.assertEqual(
                pipeline.choose_model_tier(attempt, pipeline.MECHANICAL, 0),
                pipeline.FAST_TIER,
            )

    def test_single_strong_escalation_for_test_and_semantic_from_attempt_two(self):
        for cls in (pipeline.TEST_FAILURE, pipeline.AUDIT_SEMANTIC):
            with self.subTest(failure_class=cls):
                self.assertEqual(
                    pipeline.choose_model_tier(2, cls, 0), pipeline.STRONG_TIER
                )
                self.assertEqual(
                    pipeline.choose_model_tier(3, cls, 0), pipeline.STRONG_TIER
                )
                # the per-issue cap blocks a second escalation
                self.assertEqual(
                    pipeline.choose_model_tier(2, cls, 1), pipeline.FAST_TIER
                )
        self.assertEqual(
            pipeline.choose_model_tier(2, pipeline.TEST_FAILURE, 0, max_escalations=0),
            pipeline.FAST_TIER,
        )

    def test_unknown_or_empty_failure_class_stays_fast(self):
        for cls in ("", "other", "BLOCKED"):
            self.assertEqual(pipeline.choose_model_tier(2, cls, 0), pipeline.FAST_TIER)


class TestPreAuditGate(unittest.TestCase):
    """Deterministic pre-audit gate (issue #1403); subprocess mocked."""

    @staticmethod
    def _proc(returncode=0, stdout="", stderr=""):
        return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)

    def _run_gate(self, handler, changed=("deep-agent/pipeline.py",), files=()):
        """Run the gate in a temp worktree; handler services python -m calls."""
        with tempfile.TemporaryDirectory() as root:
            for rel in files:
                path = Path(root, rel)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("def test_placeholder():\n    pass\n")

            def dispatch(argv, **kwargs):
                if argv[0] == "git":
                    return self._proc(0, "".join(f" M {p}\n" for p in changed))
                return handler(argv)

            with mock.patch.object(pipeline.subprocess, "run", side_effect=dispatch):
                return pipeline.run_preaudit_gate(root)

    def test_changed_python_files_filters_and_never_raises(self):
        porcelain = (
            " M deep-agent/pipeline.py\n"
            "M  deep-agent/coder.py\n"
            "?? notes.md\n"
            "?? tests/test_pipeline_e2e.py\n"
            'R  "old name.py" -> deep-agent/renamed.py\n'
            "?? deep-agent/__pycache__/x.pyc\n"
        )
        with mock.patch.object(
            pipeline.subprocess, "run", return_value=self._proc(0, porcelain)
        ) as run:
            self.assertEqual(
                pipeline.changed_python_files("/wt"),
                [
                    "deep-agent/coder.py",
                    "deep-agent/pipeline.py",
                    "deep-agent/renamed.py",
                    "tests/test_pipeline_e2e.py",
                ],
            )
        self.assertEqual(run.call_args.args[0][:2], ["git", "-C"])
        with mock.patch.object(
            pipeline.subprocess, "run", return_value=self._proc(1, "", "fatal")
        ):
            self.assertEqual(pipeline.changed_python_files("/wt"), [])
        with mock.patch.object(
            pipeline.subprocess, "run", side_effect=OSError("no git")
        ):
            self.assertEqual(pipeline.changed_python_files("/wt"), [])

    def test_scoped_test_files_maps_modules_and_tests(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "tests").mkdir()
            Path(root, "tests", "test_pipeline.py").write_text("x = 1\n")
            Path(root, "tests", "test_pipeline_e2e.py").write_text("x = 1\n")
            Path(root, "test_calc.py").write_text("x = 1\n")
            targets = pipeline.scoped_test_files(
                root,
                ["deep-agent/pipeline.py", "calc.py", "tests/test_pipeline_e2e.py"],
            )
        self.assertEqual(
            targets,
            ["tests/test_pipeline.py", "test_calc.py", "tests/test_pipeline_e2e.py"],
        )

    def test_no_changes_passes_without_running_checks(self):
        with mock.patch.object(
            pipeline.subprocess, "run", return_value=self._proc(0, "")
        ) as run:
            self.assertEqual(pipeline.run_preaudit_gate("/wt"), (True, "", ""))
        self.assertEqual(run.call_count, 1)  # git status only

    def test_syntax_failure_is_mechanical(self):
        def handler(argv):
            self.assertEqual(argv[2], "py_compile")
            return self._proc(1, "", "  File x.py, line 1\nSyntaxError: bad")

        ok, cls, summary = self._run_gate(handler)
        self.assertFalse(ok)
        self.assertEqual(cls, pipeline.MECHANICAL)
        self.assertIn("syntax: deep-agent/pipeline.py", summary)
        self.assertIn("SyntaxError", summary)

    def test_lint_failure_is_mechanical(self):
        def handler(argv):
            if argv[2] == "py_compile":
                return self._proc(0)
            self.assertEqual(argv[2], "ruff")
            self.assertIn("F,E4,E7,E9", argv)
            return self._proc(1, "deep-agent/pipeline.py:1:1: F401 unused")

        ok, cls, summary = self._run_gate(handler)
        self.assertFalse(ok)
        self.assertEqual(cls, pipeline.MECHANICAL)
        self.assertIn("lint:", summary)
        self.assertIn("F401", summary)

    def test_pytest_failure_is_test_failure(self):
        def handler(argv):
            if argv[2] in ("py_compile", "ruff"):
                return self._proc(0)
            self.assertEqual(argv[2], "pytest")
            self.assertIn("-x", argv)
            return self._proc(1, "FAILED tests/test_pipeline.py::test_x - assert")

        ok, cls, summary = self._run_gate(handler, files=("tests/test_pipeline.py",))
        self.assertFalse(ok)
        self.assertEqual(cls, pipeline.TEST_FAILURE)
        self.assertIn("pytest:", summary)
        self.assertIn("FAILED", summary)

    def test_missing_tools_skip_their_check(self):
        def handler(argv):
            if argv[2] == "py_compile":
                return self._proc(0)
            return self._proc(1, "", f"No module named {argv[2]}")

        ok, cls, summary = self._run_gate(handler, files=("tests/test_pipeline.py",))
        self.assertEqual((ok, cls, summary), (True, "", ""))

    def test_pytest_collecting_nothing_is_skipped(self):
        def handler(argv):
            if argv[2] in ("py_compile", "ruff"):
                return self._proc(0)
            return self._proc(5, "no tests collected")

        ok, cls, summary = self._run_gate(handler, files=("tests/test_pipeline.py",))
        self.assertEqual((ok, cls, summary), (True, "", ""))

    def test_pytest_timeout_reports_test_failure(self):
        def handler(argv):
            if argv[2] in ("py_compile", "ruff"):
                return self._proc(0)
            raise pipeline.subprocess.TimeoutExpired(cmd="pytest", timeout=300)

        ok, cls, summary = self._run_gate(handler, files=("tests/test_pipeline.py",))
        self.assertFalse(ok)
        self.assertEqual(cls, pipeline.TEST_FAILURE)
        self.assertIn("timed out", summary)

    def test_clean_run_passes(self):
        ok, cls, summary = self._run_gate(
            lambda argv: self._proc(0), files=("tests/test_pipeline.py",)
        )
        self.assertEqual((ok, cls, summary), (True, "", ""))

    def test_summary_is_head_tail_truncated(self):
        def handler(argv):
            if argv[2] == "py_compile":
                return self._proc(0)
            return self._proc(1, "E" * (pipeline.PREAUDIT_SUMMARY_LIMIT * 3))

        ok, cls, summary = self._run_gate(handler)
        self.assertFalse(ok)
        self.assertEqual(cls, pipeline.MECHANICAL)
        self.assertIn("chars omitted", summary)
        self.assertLessEqual(
            len(summary), pipeline.PREAUDIT_SUMMARY_LIMIT + len("lint: \n\n[]") + 40
        )
