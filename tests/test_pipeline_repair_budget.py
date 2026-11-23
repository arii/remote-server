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

        guard = coder.coder_tool_middleware(repair=True).wrap_tool_call
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
                guard = coder.coder_tool_middleware(repair=True).wrap_tool_call
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

        guard = coder.coder_tool_middleware(repair=True).wrap_tool_call
        handler = mock.Mock(
            return_value=ToolMessage(content="edited", tool_call_id="probe")
        )
        for n in range(coder.REPAIR_TOOL_LIMIT):
            guard(self.call("edit_file", file_path=f"{n}.py"), handler)
        with self.assertRaisesRegex(coder.RepairBudgetExceeded, "budget exhausted"):
            guard(self.call("read_file", file_path="next.py"), handler)
        self.assertEqual(handler.call_count, coder.REPAIR_TOOL_LIMIT)

    def test_initial_tool_budget_is_larger_but_cannot_be_reset_by_edits(self):
        from langchain_core.messages import ToolMessage

        self.assertGreater(coder.INITIAL_TOOL_LIMIT, coder.REPAIR_TOOL_LIMIT)
        guard = coder.coder_tool_middleware().wrap_tool_call
        handler = mock.Mock(
            return_value=ToolMessage(content="edited", tool_call_id="probe")
        )
        for n in range(coder.INITIAL_TOOL_LIMIT):
            guard(self.call("edit_file", file_path=f"{n}.py"), handler)
        with self.assertRaisesRegex(
            coder.CoderBudgetExceeded, "BLOCKED.*budget exhausted"
        ):
            guard(self.call("read_file", file_path="next.py"), handler)
        self.assertEqual(handler.call_count, coder.INITIAL_TOOL_LIMIT)

    def test_1550_initial_repeated_read_stops_before_graph_backstop(self):
        from langchain_core.messages import AIMessage, HumanMessage
        from tests.test_coder_outcome import LocalModel

        with tempfile.TemporaryDirectory() as root:
            Path(root, "tests").mkdir()
            Path(root, "tests/test_queue_runner.py").write_text("pass\n")
            model = LocalModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "read_file",
                                "args": {
                                    "file_path": "tests/test_queue_runner.py",
                                    "limit": 80,
                                },
                                "id": f"read-{n}",
                                "type": "tool_call",
                            }
                        ],
                    )
                    for n in range(20)
                ]
            )
            graph = coder.build_agent(root, "implement", model=model, repair=False)
            with self.assertRaisesRegex(
                coder.CoderBudgetExceeded, "BLOCKED.*repeated unchanged"
            ):
                graph.invoke(
                    {"messages": [HumanMessage(content="fix #1550")]},
                    config={"recursion_limit": 128},
                )
            self.assertLessEqual(model.i, 3)

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
                mock.patch("backend.shared.ledger.record"),
            ):
                graph.implement_fn("task", "plan", root, [])
            self.assertIn(notes, executor.call_args.args[1])
            self.assertTrue(executor.call_args.kwargs["repair"])
            self.assertEqual(executor.call_args.kwargs["repair_files"], ["a.py"])

    def test_read_only_repair_failure_keeps_gate_errors_and_fails_closed(self):
        empty = RuntimeError(coder.NO_WRITES)
        with tempfile.TemporaryDirectory() as root:
            graph = pipeline.Pipeline(root, "fix").make_coder(
                "### Allowed Files\n- `a.py`\n",
                worktree_root=root,
                repair_notes="gate failed",
            )
            gate_errors = ["scope violation: outside.py"]
            with (
                mock.patch.object(coder, "run_coder", side_effect=empty),
                mock.patch("backend.shared.ledger.record"),
            ):
                with self.assertRaises(RuntimeError) as repaired:
                    graph.implement_fn("task", "plan", root, gate_errors)
                with self.assertRaises(RuntimeError) as first_pass:
                    graph.implement_fn("task", "plan", root, [])
        self.assertIn(coder.NO_WRITES, str(repaired.exception))
        self.assertIn("scope violation: outside.py", str(repaired.exception))
        self.assertEqual(str(first_pass.exception), coder.NO_WRITES)

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


class TestModelTierSelector(unittest.TestCase):
    def test_first_attempt_defaults_to_fast_tier(self):
        tier, used = pipeline.choose_model_tier(
            attempt=1, failure_class=None, escalations_used=0, max_escalations=1
        )
        self.assertEqual(tier, "fast")
        self.assertEqual(used, 0)

        # Even with prior failure class, attempt 1 stays on fast tier
        tier, used = pipeline.choose_model_tier(
            attempt=1,
            failure_class="test_failure",
            escalations_used=0,
            max_escalations=1,
        )
        self.assertEqual(tier, "fast")
        self.assertEqual(used, 0)

    def test_mechanical_failure_stays_on_fast_tier(self):
        for attempt in (1, 2, 3, 4):
            tier, used = pipeline.choose_model_tier(
                attempt=attempt,
                failure_class="mechanical",
                escalations_used=0,
                max_escalations=1,
            )
            self.assertEqual(tier, "fast")
            self.assertEqual(used, 0)

    def test_test_failure_escalates_to_strong_tier_once(self):
        # Attempt 2 escalates to strong
        tier, used = pipeline.choose_model_tier(
            attempt=2,
            failure_class="test_failure",
            escalations_used=0,
            max_escalations=1,
        )
        self.assertEqual(tier, "strong")
        self.assertEqual(used, 1)

        # Subsequent retry is capped by max_escalations=1, stays on fast
        tier, used = pipeline.choose_model_tier(
            attempt=3,
            failure_class="test_failure",
            escalations_used=1,
            max_escalations=1,
        )
        self.assertEqual(tier, "fast")
        self.assertEqual(used, 1)

    def test_audit_semantic_failure_escalates_to_strong_tier_once(self):
        tier, used = pipeline.choose_model_tier(
            attempt=2,
            failure_class="audit_semantic",
            escalations_used=0,
            max_escalations=1,
        )
        self.assertEqual(tier, "strong")
        self.assertEqual(used, 1)

        tier, used = pipeline.choose_model_tier(
            attempt=3,
            failure_class="audit_semantic",
            escalations_used=1,
            max_escalations=1,
        )
        self.assertEqual(tier, "fast")
        self.assertEqual(used, 1)


class TestDeterministicPreAuditGate(unittest.TestCase):
    def test_pre_audit_catches_ast_syntax_error_as_mechanical(self):
        with tempfile.TemporaryDirectory() as root:
            # Create a broken py file with invalid syntax
            broken_file = Path(root, "broken.py")
            broken_file.write_text("def broken_func(\n")
            with mock.patch("subprocess.run") as mock_run:
                mock_run.return_value = SimpleNamespace(
                    stdout="broken.py\n", stderr="", returncode=0
                )
                pipe = pipeline.Pipeline(root, "fix syntax")
                passed, f_class, reason = pipe.pre_audit_check(root)
            self.assertFalse(passed)
            self.assertEqual(f_class, "mechanical")
            self.assertIn("SyntaxError", reason)

    def test_pre_audit_fails_closed_when_changed_file_listing_fails(self):
        with tempfile.TemporaryDirectory() as root:
            pipe = pipeline.Pipeline(root, "fix syntax")
            with mock.patch("subprocess.run") as mock_run:
                mock_run.return_value = SimpleNamespace(
                    stdout="",
                    stderr="fatal: bad revision 'origin/main'",
                    returncode=128,
                )
                passed, f_class, reason = pipe.pre_audit_check(root)
            self.assertFalse(passed)
            self.assertEqual(f_class, "mechanical")
            self.assertIn("could not list changed files", reason)
            with mock.patch("subprocess.run", side_effect=OSError("no git")):
                passed, f_class, reason = pipe.pre_audit_check(root)
            self.assertFalse(passed)
            self.assertEqual(f_class, "mechanical")
            self.assertIn("could not list changed files", reason)

    def test_pre_audit_catches_verification_command_failure_as_test_failure(self):
        with tempfile.TemporaryDirectory() as root:
            pipe = pipeline.Pipeline(
                root,
                "fix tests",
                verification_commands=[["python3", "-m", "pytest", "tests/"]],
            )
            with (
                mock.patch("subprocess.run") as mock_run,
                mock.patch("pathlib.Path.is_file", return_value=False),
            ):
                mock_run.side_effect = [
                    # git diff proc
                    SimpleNamespace(stdout="", stderr="", returncode=0),
                    # verification command proc
                    SimpleNamespace(
                        stdout="FAILED test_something.py::test_calc\n",
                        stderr="",
                        returncode=1,
                    ),
                ]
                passed, f_class, reason = pipe.pre_audit_check(root)
            self.assertFalse(passed)
            self.assertEqual(f_class, "test_failure")
            self.assertIn("FAILED test_something.py::test_calc", reason)

    def test_patch_failing_pre_audit_routes_back_to_coder_skipping_auditor(self):
        with tempfile.TemporaryDirectory() as root:
            coder_calls = []
            audit_mock = mock.Mock()

            def fake_code(brief, worktree_root=None, repair_notes=""):
                coder_calls.append((worktree_root, repair_notes))
                return True, {}, root

            pre_audit_outcomes = [
                (False, "mechanical", "SyntaxError: unexpected EOF"),
                (True, None, ""),
            ]

            def fake_pre_audit(worktree_root):
                return pre_audit_outcomes.pop(0)

            pipe = pipeline.Pipeline(
                root,
                "fix helper",
                pre_audit_fn=fake_pre_audit,
                audit_fn=audit_mock,
                max_repair_cycles=2,
            )
            pipe.stage_init = lambda: (True, "ready")
            pipe.stage_spec = lambda: (True, {}, "spec")
            pipe.stage_code = fake_code
            pipe.stage_audit = lambda wt: (pipeline.APPROVED, {}, "approved")
            pipe.stage_cleanup = lambda wt: (True, "")

            result = pipe.run()
            self.assertEqual(result["status"], pipeline.APPROVED)
            self.assertEqual(len(coder_calls), 2)
            # The first repair attempt receives the pre-audit error notes
            self.assertIn("SyntaxError: unexpected EOF", coder_calls[1][1])
            # Auditor LLM was never called during the first pre-audit failure
            audit_mock.assert_not_called()

    def test_pipeline_terminates_cleanly_when_repair_cycles_exhausted(self):
        with tempfile.TemporaryDirectory() as root:

            def fake_pre_audit(worktree_root):
                return False, "mechanical", "SyntaxError: unfixable"

            pipe = pipeline.Pipeline(
                root,
                "stuck issue",
                pre_audit_fn=fake_pre_audit,
                max_repair_cycles=2,
            )
            pipe.stage_init = lambda: (True, "ready")
            pipe.stage_spec = lambda: (True, {}, "spec")
            pipe.stage_code = lambda brief, worktree_root=None, repair_notes="": (
                True,
                {},
                root,
            )
            result = pipe.run()
            self.assertEqual(result["status"], pipeline.CHANGES_REQUESTED)
            self.assertIn("repair cycles exhausted (2/2)", result["detail"])
