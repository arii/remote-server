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
